"""AILeadResearchAgent — pipeline orchestration tests."""

from __future__ import annotations

import json

from app.lead_research.agent import (
    AILeadResearchAgent,
    _is_free_mail,
    _is_generic_local_part,
    _is_placeholder_or_fake_domain,
)

# Deterministic test stub — never a real MX lookup offline.
_MX_OK = lambda d: True  # noqa: E731
from app.lead_research.company_research import CompanyResearcher
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import CompanyProfile, PersonFindings
from app.lead_research.person_research_ai import PersonResearcherAI
from app.lead_research.scoring import LeadScorer
from tests.lead_research.conftest import make_fake_ai, make_fake_fetch, make_fake_refine, make_fake_search


# ---------------------------------------------------------------------------
# Triage helpers
# ---------------------------------------------------------------------------

def test_is_free_mail():
    assert _is_free_mail("gmail.com") is True
    assert _is_free_mail("yahoo.com") is True
    assert _is_free_mail("acme.com") is False


def test_is_generic_local_part():
    assert _is_generic_local_part("info@acme.com") is True
    assert _is_generic_local_part("office@acme.com") is True
    assert _is_generic_local_part("john@acme.com") is False


def test_is_placeholder_or_fake_domain_catches_reserved_placeholders():
    """Reserved/placeholder domains (RFC 2606 + common scraped junk) are junk."""
    assert _is_placeholder_or_fake_domain("example.com") is True
    assert _is_placeholder_or_fake_domain("example.org") is True
    assert _is_placeholder_or_fake_domain("test.com") is True
    assert _is_placeholder_or_fake_domain("invalid") is True
    assert _is_placeholder_or_fake_domain("localhost") is True


def test_is_placeholder_or_fake_domain_catches_file_extension_artifacts():
    """The observed 'logo@3x-1-236x60.png' class — a filename, not a mailbox."""
    assert _is_placeholder_or_fake_domain("3x-1-236x60.png") is True
    assert _is_placeholder_or_fake_domain("about_us.pdf") is True
    assert _is_placeholder_or_fake_domain("team-logo.svg") is True
    assert _is_placeholder_or_fake_domain("documents.zip") is True


def test_is_placeholder_or_fake_domain_allows_real_domains():
    """A real TLD with a file-extension-looking sub-label is NOT caught:
    only the LAST label counts as the TLD."""
    assert _is_placeholder_or_fake_domain("png.com") is False
    assert _is_placeholder_or_fake_domain("examp1e.com") is False
    assert _is_placeholder_or_fake_domain("redhawkservices.us") is False


def test_is_placeholder_or_fake_domain_empty_is_junk():
    assert _is_placeholder_or_fake_domain("") is True
    assert _is_placeholder_or_fake_domain("   ") is True


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def _make_agent(ai_response=None):
    """Build an agent with all fake seams."""
    if ai_response is None:
        ai_response = {
            "company_name": "Acme Construction",
            "industry": "General Contractor",
            "location": "Dallas, TX",
            "website": "https://acme.com",
            "facts": [
                {"claim": "Estimating services needed", "source_url": "https://acme.com", "source_type": "website", "confidence": "verified"},
            ],
        }

    company = CompanyResearcher(
        ai_ask=make_fake_ai(ai_response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe",
            "person_role": "Project Manager",
            "role_relevance": True,
            "bound": True,
            "evidence": [{"claim": "Team page", "source_url": "https://acme.com/team", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes",
        "signal": "Active bids",
        "reason": "Regularly bids",
        "evidence": [{"claim": "Bid activity", "source_url": "https://acme.com/bids", "source_type": "website", "confidence": "verified"}],
        "timing_window": "now",
        "timing_reason": "Q1 season",
        "timing_events": [{"claim": "RFP open", "source_url": "", "source_type": "inferred", "confidence": "unverified"}],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "Strong fit — active GC",
        "potential_score": 7.5,
        "recommendation": "contact_now",
        "reasoning": "Good match",
    }))

    return AILeadResearchAgent(
        company_researcher=company,
        person_researcher=person,
        intent_analyzer=intent,
        scorer=scorer,
        domain_delivers_email=_MX_OK,
    )


def test_full_pipeline_happy_path():
    agent = _make_agent()
    dossier = agent.research("jane@acme.com", "acme.com")

    assert dossier.email == "jane@acme.com"
    assert dossier.refined_domain == "acme.com"
    assert dossier.company.name == "Acme Construction"
    assert dossier.person.name == "Jane Doe"
    assert dossier.person.bound is True
    assert dossier.intent.needs_estimation == "yes"
    assert dossier.timing.window == "now"
    assert dossier.potential_score >= 6.0  # deterministic score for strong-fit lead
    assert dossier.recommendation == "contact_now"
    assert "company_research" in dossier.sources_checked
    assert "person_research" in dossier.sources_checked
    assert "intent_timing" in dossier.sources_checked
    assert "scoring" in dossier.sources_checked
    assert dossier.source_errors == {}


def test_triage_free_mail_goes_to_nurture():
    agent = _make_agent()
    dossier = agent.research("john@gmail.com", "gmail.com")
    assert dossier.recommendation == "nurture"
    assert "free mail" in dossier.fit.lower()
    assert dossier.sources_checked == []


def test_triage_generic_email_runs_company_research_skips_person():
    """info@ on a REAL business domain runs company research (the company may
    be a valid construction firm) but skips person research (no specific
    individual to find).  Scoring treats it as 'generic company contact' and
    the construction company reaches nurture tier."""
    agent = _make_agent()
    dossier = agent.research("info@acme.com", "acme.com")
    # Company research ran — we identified the company
    assert dossier.company.name == "Acme Construction"
    assert "company_research" in dossier.sources_checked
    # Person research was skipped (generic email, no specific person)
    assert "person_research" not in dossier.sources_checked
    assert dossier.person.name == ""  # default — no person research
    # Scoring: construction 2.0 + generic_company_contact 0.5 + facts 0.5 = 3.0 → nurture
    assert dossier.recommendation == "nurture"
    assert dossier.potential_score >= 3.0


def test_junk_placeholder_domain_rejected_before_any_research():
    """yourname@example.com (observed in a live run) is rejected outright: the
    AI pipeline never runs for it, even when the caller passes a plausible
    registered-domain (the extractor may set that from a nearby company)."""
    calls = {"research": 0}

    company = CompanyResearcher(
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("example.com"),
    )
    company.research_with_domain = lambda *a, **k: calls.__setitem__("research", calls["research"] + 1)

    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({}))
    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=LeadScorer(),
        domain_delivers_email=_MX_OK,
    )
    # registered-domain is a plausible "example.com" — the junk is in the EMAIL.
    dossier = agent.research("yourname@example.com", "example.com")
    assert dossier.recommendation == "skip"
    assert "junk" in dossier.fit.lower()
    assert dossier.sources_checked == []
    assert calls["research"] == 0  # AI pipeline never started


def test_junk_file_extension_domain_rejected_before_any_research():
    """logo@3x-1-236x60.png (observed: an image filename parsed as an email,
    which previously got a nurture recommendation) is rejected outright, even
    when the caller passes a real company registered-domain like hittcontracting
    — the junk is the address's own @-domain."""
    calls = {"research": 0}

    company = CompanyResearcher(
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("hittcontracting.com"),
    )
    company.research_with_domain = lambda *a, **k: calls.__setitem__("research", calls["research"] + 1)

    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({}))
    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=LeadScorer(),
        domain_delivers_email=_MX_OK,
    )
    dossier = agent.research("logo@3x-1-236x60.png", "hittcontracting.com")
    assert dossier.recommendation == "skip"
    assert "junk" in dossier.fit.lower()
    assert dossier.sources_checked == []
    assert calls["research"] == 0


def test_empty_domain_free_mail_is_derived_and_triaged():
    """A free-mail lead with an empty registered-domain field is still triaged
    to nurture (domain derived from the email) instead of running the full
    pipeline — which could otherwise bind a name from the local part."""
    agent = _make_agent()
    dossier = agent.research("dadoduffy@aol.com", "")
    assert dossier.recommendation == "nurture"
    assert "free mail" in dossier.fit.lower()
    assert dossier.domain == "aol.com"
    assert dossier.sources_checked == []


def test_dead_domain_skips_before_any_research():
    """A domain that resolves no MX (dead/expired) is skipped outright — the
    expensive AI company/person pipeline never runs for it."""
    calls = {"research": 0}

    company = CompanyResearcher(
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("deadcoyote.com"),
    )
    # Spy: the company lane must never run once the dead domain is caught.
    company.research_with_domain = lambda *a, **k: calls.__setitem__("research", calls["research"] + 1)

    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({}))
    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=LeadScorer(),
        domain_delivers_email=lambda d: False,  # dead domain
    )
    dossier = agent.research("jane@deadcoyote.com", "deadcoyote.com")
    assert dossier.recommendation == "skip"
    assert "dead" in dossier.fit.lower()
    assert dossier.sources_checked == []
    assert calls["research"] == 0  # AI pipeline never started


def test_construction_context_threaded_to_company_research():
    """The query's trade/location (construction-bid provenance) is threaded
    into the company researcher so the AI anchors on construction instead of
    drifting to generic/IT results."""
    seen: dict = {}

    company = CompanyResearcher(
        ai_ask=make_fake_ai({
            "company_name": "Acme Electric",
            "industry": "Electrical Contractor",
            "location": "Houston, TX",
            "website": "https://acme.com",
            "facts": [],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    company.research_with_domain = lambda email, domain, **kw: seen.__setitem__("kw", kw)

    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe", "person_role": "Owner", "role_relevance": True, "bound": True,
            "evidence": [{"claim": "Owner", "source_url": "https://acme.com/about", "source_type": "about_page", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes", "signal": "", "reason": "", "evidence": [],
        "timing_window": "unknown", "timing_reason": "", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "fit", "potential_score": 7.0, "recommendation": "contact_now", "reasoning": "",
    }))

    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer, domain_delivers_email=_MX_OK,
    )
    agent.research("jane@acme.com", "acme.com", trade="Electrical", location="Houston TX")

    assert seen.get("kw", {}).get("trade") == "Electrical"
    assert seen.get("kw", {}).get("location") == "Houston TX"


def test_company_ai_failure_still_runs_pipeline():
    """Stage 1 AI failure → graceful degradation, pipeline continues."""
    company = CompanyResearcher(
        ai_ask=lambda p: (_ for _ in ()).throw(RuntimeError("AI down")),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane", "person_role": "PM", "role_relevance": True, "bound": True,
            "evidence": [{"claim": "Found", "source_url": "https://acme.com", "source_type": "website", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes", "signal": "", "reason": "", "evidence": [],
        "timing_window": "unknown", "timing_reason": "", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "partial", "potential_score": 4.0, "recommendation": "nurture", "reasoning": "",
    }))

    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer, domain_delivers_email=_MX_OK,
    )
    dossier = agent.research("jane@acme.com", "acme.com")

    # Company AI failed gracefully → partial company data, no exception
    assert dossier.company.name == ""
    assert "AI call failed" in dossier.company.facts[0].claim
    # But other stages still ran
    assert "person_research" in dossier.sources_checked
    assert "intent_timing" in dossier.sources_checked
    assert "scoring" in dossier.sources_checked


def test_person_ai_failure_still_scores():
    """Stage 2 AI failure → graceful degradation, scoring still runs."""
    agent = _make_agent()
    # Override person to fail
    agent._person = PersonResearcherAI(
        deterministic=None,
        ai_ask=lambda p: (_ for _ in ()).throw(RuntimeError("person AI down")),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    dossier = agent.research("jane@acme.com", "acme.com")
    # Person AI failed gracefully → partial person data
    assert dossier.person.name == ""
    assert "AI call failed" in dossier.person.evidence[0].claim
    # Scoring still ran
    assert "scoring" in dossier.sources_checked


def test_dossier_to_dict_roundtrip():
    """Dossier from agent can be serialized and deserialized."""
    agent = _make_agent()
    dossier = agent.research("jane@acme.com", "acme.com")
    d = dossier.to_dict()
    from app.lead_research.models import LeadDossier
    dossier2 = LeadDossier.from_dict(d)
    assert dossier2.email == dossier.email
    assert dossier2.company.name == dossier.company.name
    assert dossier2.potential_score == dossier.potential_score


def test_deep_research_recorded_even_when_empty():
    """Deep-research lane shows in sources_checked even if it finds no signals."""
    # Company returns no deep facts (simulates 'no growth signals found')
    company = CompanyResearcher(
        ai_ask=make_fake_ai({
            "company_name": "Acme Construction",
            "industry": "General Contractor",
            "location": "Dallas, TX",
            "website": "https://acme.com",
            "facts": [{"claim": "GC", "source_url": "https://acme.com/about", "source_type": "about_page", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    # Spy on research_deep so the real multi-key lane is NOT invoked
    original = company.research_deep
    # Agent threads `location` into research_deep (state-aware license queries).
    company.research_deep = lambda domain, company_name, **kwargs: []  # no signals
    assert original is not None  # sanity: method existed

    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe", "person_role": "Owner", "role_relevance": True, "bound": True,
            "evidence": [{"claim": "Owner", "source_url": "https://acme.com/about", "source_type": "about_page", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "yes", "signal": "", "reason": "", "evidence": [],
        "timing_window": "unknown", "timing_reason": "", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "partial", "potential_score": 5.0, "recommendation": "nurture", "reasoning": "",
    }))

    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer, domain_delivers_email=_MX_OK,
    )
    dossier = agent.research("jane@acme.com", "acme.com")
    assert dossier.company.name == "Acme Construction"
    assert dossier.person.bound is True
    # Deep lane was attempted and recorded, even though it found nothing
    assert "deep_research" in dossier.sources_checked
    assert dossier.source_errors.get("deep_research") is None


def test_deep_research_fires_without_bound_person():
    """Opened gate: deep research examines the COMPANY (licence/news/hiring),
    so it must run for an on-vertical construction company with a name even when
    no decision-maker is bound (person.bound False). This closes the 'sirf ek AI
    chala' gap — runs where discovery surfaced a company but no person no longer
    starve the second AI key."""
    company = CompanyResearcher(
        ai_ask=make_fake_ai({
            "company_name": "Acme Construction",
            "industry": "General Contractor",
            "location": "Dallas, TX",
            "website": "https://acme.com",
            "facts": [{"claim": "GC", "source_url": "https://acme.com/about", "source_type": "about_page", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    deep_calls = {"n": 0}
    original = company.research_deep
    assert original is not None  # sanity
    company.research_deep = lambda domain, company_name, **kwargs: (
        deep_calls.__setitem__("n", deep_calls["n"] + 1) or []
    )
    # Person stage returns NO decision-maker (bound=False) — the old gate would
    # have skipped deep research here.
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "", "person_role": "", "role_relevance": False,
            "bound": False,
            "evidence": [],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "no", "signal": "", "reason": "", "evidence": [],
        "timing_window": "unknown", "timing_reason": "", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "partial", "potential_score": 5.0, "recommendation": "nurture", "reasoning": "",
    }))

    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer, domain_delivers_email=_MX_OK,
    )
    dossier = agent.research("jane@acme.com", "acme.com")
    assert dossier.company.name == "Acme Construction"
    assert dossier.person.bound is False          # no decision-maker bound
    assert deep_calls["n"] == 1                    # deep lane STILL fired
    assert "deep_research" in dossier.sources_checked


def test_deep_research_still_gated_off_on_off_vertical():
    """Opening the person gate must NOT remove the client-fit gate: an
    off-vertical company (e.g. fiber/telecom, never our client) still never
    spends deep-research credits."""
    company = CompanyResearcher(
        ai_ask=make_fake_ai({
            "company_name": "Acme Fiber",
            "industry": "Telecommunications",
            "location": "Dallas, TX",
            "website": "https://acme.com",
            "facts": [],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    deep_calls = {"n": 0}
    original = company.research_deep
    assert original is not None
    company.research_deep = lambda domain, company_name, **kwargs: (
        deep_calls.__setitem__("n", deep_calls["n"] + 1) or []
    )
    person = PersonResearcherAI(
        deterministic=None,
        ai_ask=make_fake_ai({
            "person_name": "Jane Doe", "person_role": "Owner", "role_relevance": True, "bound": True,
            "evidence": [{"claim": "Owner", "source_url": "https://acme.com/about", "source_type": "about_page", "confidence": "verified"}],
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
    )
    intent = IntentTimingAnalyzer(ai_ask=make_fake_ai({
        "needs_estimation": "no", "signal": "", "reason": "", "evidence": [],
        "timing_window": "unknown", "timing_reason": "", "timing_events": [],
    }))
    scorer = LeadScorer(ai_ask=make_fake_ai({
        "fit": "partial", "potential_score": 5.0, "recommendation": "nurture", "reasoning": "",
    }))

    agent = AILeadResearchAgent(
        company_researcher=company, person_researcher=person,
        intent_analyzer=intent, scorer=scorer, domain_delivers_email=_MX_OK,
    )
    dossier = agent.research("jane@acme.com", "acme.com")
    assert deep_calls["n"] == 0
    assert "deep_research" not in dossier.sources_checked
