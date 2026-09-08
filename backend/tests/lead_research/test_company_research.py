"""CompanyResearcher — deterministic tests with injected seams."""

from __future__ import annotations

import json

from app.lead_research.company_research import (
    CompanyResearcher,
    _parse_ai_json,
    _truncate_html,
    default_refine_domain,
)
from tests.lead_research.conftest import (
    make_fake_ai,
    make_fake_fetch,
    make_fake_refine,
    make_fake_search,
)


# ---------------------------------------------------------------------------
# _parse_ai_json
# ---------------------------------------------------------------------------

def test_parse_json_clean():
    assert _parse_ai_json('{"key": "value"}') == {"key": "value"}


def test_parse_json_with_fences():
    raw = '```json\n{"key": "value"}\n```'
    assert _parse_ai_json(raw) == {"key": "value"}


def test_parse_json_with_fences_no_lang():
    raw = '```\n{"key": "value"}\n```'
    assert _parse_ai_json(raw) == {"key": "value"}


def test_parse_json_garbage_returns_empty():
    assert _parse_ai_json("this is not json at all") == {}


# ---------------------------------------------------------------------------
# _truncate_html
# ---------------------------------------------------------------------------

def test_truncate_html_strips_tags():
    html = "<html><body>Hello <b>world</b></body></html>"
    result = _truncate_html(html)
    assert "<" not in result
    assert "Hello" in result
    assert "world" in result


def test_truncate_html_removes_script():
    html = "<script>evil();</script><body>safe</body>"
    result = _truncate_html(html)
    assert "evil" not in result
    assert "safe" in result


def test_truncate_html_limits_length():
    html = "<p>" + "x" * 10000 + "</p>"
    result = _truncate_html(html, max_chars=500)
    assert len(result) <= 500


# ---------------------------------------------------------------------------
# default_refine_domain
# ---------------------------------------------------------------------------

def test_refine_domain_already_valid():
    """Domain with MX → keep as-is."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"

    result = default_refine_domain("acme.com", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_fixes_typo():
    """Typo TLD (.comz) → corrected to .com."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"  # .comz won't match, .com will

    result = default_refine_domain("acme.comz", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_strips_www():
    """www.acme.com → acme.com."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"

    result = default_refine_domain("www.acme.com", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_strips_scheme():
    """https://acme.com/path → acme.com."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"

    result = default_refine_domain("https://acme.com/some/path", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_no_mx_returns_normalized():
    """No MX resolves → return normalized original."""
    result = default_refine_domain("acme.xyz", mx_check=lambda d: False)
    assert result == "acme.xyz"


def test_refine_domain_empty_returns_original():
    result = default_refine_domain("", mx_check=lambda d: False)
    assert result == ""


# ---------------------------------------------------------------------------
# CompanyResearcher.research
# ---------------------------------------------------------------------------

def _good_ai_response():
    return {
        "company_name": "Acme Construction",
        "industry": "General Contractor",
        "location": "Dallas, TX",
        "website": "https://acme.com",
        "facts": [
            {"claim": "Founded in 1990", "source_url": "https://acme.com/about", "source_type": "website", "confidence": "verified"},
            {"claim": "Revenue ~$10M", "source_url": "", "source_type": "inferred", "confidence": "unverified"},
        ],
    }


def test_research_happy_path():
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == "Acme Construction"
    assert profile.industry == "General Contractor"
    assert profile.location == "Dallas, TX"
    assert len(profile.facts) == 2
    assert profile.facts[0].confidence == "verified"
    assert profile.facts[1].confidence == "unverified"


def test_research_parses_client_verdict():
    """The Stage-1 client-fit verdict (is_our_client + client_reason) is parsed
    from the SAME research call — no extra AI round-trip."""
    resp = _good_ai_response()
    resp["is_our_client"] = "no"
    resp["client_reason"] = "Engineering consultancy, not a bidding contractor."
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(resp),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.is_our_client == "no"
    assert profile.client_reason == "Engineering consultancy, not a bidding contractor."


def test_research_normalizes_unknown_verdict():
    """A verdict the model returns that is not a clean yes/no/unsure collapses to
    "unsure" (default-keep, never a silent skip); a missing verdict stays empty."""
    resp = _good_ai_response()
    resp["is_our_client"] = "MAYBE"  # not a clean token
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(resp),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    assert researcher.research("john@acme.com", "acme.com").is_our_client == "unsure"

    # Absent verdict → empty (an old-style response never fabricates a verdict).
    researcher2 = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    assert researcher2.research("john@acme.com", "acme.com").is_our_client == ""


def test_research_ai_returns_empty_json():
    """AI returns empty JSON {} → treated as no data, partial profile with error fact."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == ""
    # Empty dict {} is valid JSON but means no useful data → unparseable error
    assert len(profile.facts) == 1
    assert "unparseable" in profile.facts[0].claim.lower()
    """AI returns non-JSON → partial profile, no crash."""
    researcher = CompanyResearcher(
        ai_ask=lambda prompt: "this is not json",
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == ""
    assert len(profile.facts) == 1
    assert "unparseable" in profile.facts[0].claim.lower()


def test_research_ai_raises_exception():
    """AI call throws → partial profile with error fact."""
    def boom(prompt: str) -> str:
        raise RuntimeError("API down")

    researcher = CompanyResearcher(
        ai_ask=boom,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == ""
    assert "failed" in profile.facts[0].claim.lower()


def test_research_includes_refined_domain_in_prompt():
    """Verify refined domain (not original) is used in the prompt."""
    captured = {}

    def capturing_ai(prompt: str) -> str:
        captured["prompt"] = prompt
        return json.dumps(_good_ai_response())

    researcher = CompanyResearcher(
        ai_ask=capturing_ai,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),  # refines from acme.comz
    )
    researcher.research("john@acme.comz", "acme.comz")
    assert "acme.com" in captured["prompt"]
    assert "john@acme.comz" in captured["prompt"]


def test_research_gathers_multiple_search_queries():
    """Multiple search queries are run and deduplicated."""
    seen = []

    def tracking_search(query: str) -> list[dict[str, str]]:
        seen.append(query)
        return []

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=tracking_search,
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    researcher.research("john@acme.com", "acme.com")
    assert len(seen) == 4  # screening: email, domain, company, LinkedIn (BBB dork dropped — 1% yield)


def test_research_fetches_home_about_contact():
    """Fetches homepage, /about, and /contact pages."""
    fetched_urls = []

    def tracking_fetch(url: str):
        fetched_urls.append(url)
        mock = type("M", (), {"ok": True, "html": "<html>page</html>"})()
        return mock

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=tracking_fetch,
        refine_domain=make_fake_refine("acme.com"),
    )
    researcher.research("john@acme.com", "acme.com")
    assert "https://acme.com" in fetched_urls
    assert "https://acme.com/about" in fetched_urls
    assert "https://acme.com/contact" in fetched_urls


def test_research_fetch_failure_doesnt_crash():
    """Fetch failures are silently skipped."""
    def fail_fetch(url: str):
        raise ConnectionError("timeout")

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=fail_fetch,
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == "Acme Construction"


def test_research_empty_search_results():
    """Empty search results → still works."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search([]),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == "Acme Construction"


def test_research_unreadable_site_without_citations_wipes_identity():
    """R3 regression (the nsarro@unitedcr.com bug): when the site is unreadable
    AND the AI returned no source-bearing fact (nothing it could actually have
    seen), a name/industry/location is fabrication — wipe the identity block so
    the company reads 'unknown' and can never be scored as a real contractor."""
    ai_response = {
        "company_name": "United Construction Company",
        "industry": "Insurance Restoration Contractor",  # fabricated identity
        "location": "Somewhere",
        "website": "https://unitedcr.com",
        "facts": [
            {"claim": "Player in restoration", "source_url": "", "source_type": "inferred", "confidence": "unverified"},
        ],
    }
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(ai_response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(pages={}),  # every page → unreadable
        refine_domain=make_fake_refine("unitedcr.com"),
    )
    profile = researcher.research("nsarro@unitedcr.com", "unitedcr.com")
    assert profile.name == ""      # wiped
    assert profile.industry == ""  # wiped
    assert profile.location == ""  # wiped


def test_research_unreadable_site_keeps_identity_when_cited():
    """Same unreadable site, but the AI DID produce a source-bearing fact —
    that citation is the evidence trail, so identity survives the guard."""
    ai_response = {
        "company_name": "United Construction Company",
        "industry": "General Contractor",
        "location": "Houston, TX",
        "website": "https://unitedcr.com",
        "facts": [
            {"claim": "Founded 1988", "source_url": "https://search-result.com/unitedcr", "source_type": "search_result", "confidence": "verified"},
        ],
    }
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(ai_response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(pages={}),  # every page → unreadable
        refine_domain=make_fake_refine("unitedcr.com"),
    )
    profile = researcher.research("nsarro@unitedcr.com", "unitedcr.com")
    assert profile.name == "United Construction Company"  # kept
    assert profile.industry == "General Contractor"        # kept
    assert len(profile.facts) == 1
    assert profile.facts[0].source_url  # the citation that kept it honest


def test_research_with_domain_returns_dict():
    """research_with_domain returns dict including refined_domain."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    result = researcher.research_with_domain("john@acme.comz", "acme.comz")
    assert result["refined_domain"] == "acme.com"
    assert result["original_domain"] == "acme.comz"
    assert result["name"] == "Acme Construction"


# ---------------------------------------------------------------------------
# CompanyResearcher.research_deep  (Stage 1b / multi-key lane)
# ---------------------------------------------------------------------------

def test_research_deep_returns_evidence():
    """research_deep runs deep queries and returns cited AIEvidence."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai({
            "facts": [
                {"claim": "Hiring estimator (LinkedIn)", "source_url": "https://linkedin.com/jobs/x", "source_type": "search_result", "confidence": "verified"},
                {"claim": "Won bid 2 days ago", "source_url": "https://news.com/award", "source_type": "news", "confidence": "verified"},
            ]
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    facts = researcher.research_deep("acme.com", "Acme Construction")
    assert len(facts) == 2
    assert facts[0].claim == "Hiring estimator (LinkedIn)"
    assert facts[0].confidence == "verified"
    assert facts[1].source_url == "https://news.com/award"


def test_research_deep_runs_deep_queries():
    """research_deep issues the 6 deep-dive queries (not the 5 screening ones)."""
    seen = []

    def tracking_search(query: str) -> list[dict[str, str]]:
        seen.append(query)
        return []

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai({"facts": []}),
        search=tracking_search,
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    researcher.research_deep("acme.com", "Acme Construction")
    assert len(seen) == 5  # state-aware license + 4 growth signals
    # Deep queries target growth/need signals, not identity screening
    joined = " ".join(seen).lower()
    assert "license" in joined
    assert "hiring" in joined
    assert "bid" in joined or "award" in joined


def test_research_deep_reuses_injected_ai():
    """Injected ai_ask is reused for the deep lane (test path of multi-key)."""
    calls = []

    def capturing_ai(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps({"facts": [{"claim": "expanding", "source_url": "", "source_type": "other", "confidence": "unverified"}]})

    researcher = CompanyResearcher(
        ai_ask=capturing_ai,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    researcher.research_deep("acme.com", "Acme Construction")
    assert len(calls) == 1
    assert "Acme Construction" in calls[0]


def test_research_deep_ai_error_returns_empty():
    """Deep-research AI failure returns [] (additive, never fatal)."""
    def boom(prompt: str) -> str:
        raise RuntimeError("deep key down")

    researcher = CompanyResearcher(
        ai_ask=boom,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    assert researcher.research_deep("acme.com", "Acme Construction") == []


# ---------------------------------------------------------------------------
# Phase E — labeled page blocks + LinkedIn company lane + exact-page guard
# ---------------------------------------------------------------------------

def test_gather_site_labels_pages_with_real_urls():
    """The exact-page fix: every fetched page is a LABELED block stamped with
    its real URL + human name, so the AI can cite ``/about`` instead of the
    bare domain root."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    content = researcher._gather_site(make_fake_fetch({
        "https://acme.com": "<html>Acme Construction</html>",
        "https://acme.com/about": "<html>About Acme Construction</html>",
        "https://acme.com/contact": "<html>Contact us</html>",
        "https://acme.com/services": "<html>Services</html>",
    }), "acme.com")
    assert "[PAGE: Homepage — https://acme.com]" in content
    assert "[PAGE: About us — https://acme.com/about]" in content
    assert "[PAGE: Contact — https://acme.com/contact]" in content


def test_linkedin_company_lane_extracts_and_labels_in_prompt(monkeypatch):
    """A linkedin.com/company page surfaced in search results is extracted and
    injected as a labeled block — the AI can cite company activity on LinkedIn
    (the plan-approved lane), source_type 'linkedin' downstream."""
    monkeypatch.delenv("LINKEDIN_LANE_ENABLED", raising=False)
    captured = {}
    li_url = "https://www.linkedin.com/company/acme-construction"

    def capturing_ai(prompt: str) -> str:
        captured["prompt"] = prompt
        return json.dumps(_good_ai_response())

    researcher = CompanyResearcher(
        ai_ask=capturing_ai,
        search=make_fake_search([
            {"url": li_url, "title": "Acme Construction", "snippet": "Commercial GC"},
            {"url": "https://example.com/about", "title": "About", "snippet": ""},
        ]),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
        linkedin_extract=lambda url: "Latest posts: hired 2 estimators, opened Houston office.",
    )
    researcher.research("john@acme.com", "acme.com")
    assert f"[PAGE: LinkedIn — {li_url}]" in captured["prompt"]
    assert "hired 2 estimators" in captured["prompt"]


def test_linkedin_company_lane_skipped_when_disabled(monkeypatch):
    """LINKEDIN_LANE_ENABLED=0 disables the extract lane entirely."""
    monkeypatch.setenv("LINKEDIN_LANE_ENABLED", "0")
    called = []

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search([
            {"url": "https://www.linkedin.com/company/acme-construction", "title": "Acme", "snippet": ""},
        ]),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
        linkedin_extract=lambda url: (called.append(url), "content")[1],
    )
    researcher.research("john@acme.com", "acme.com")
    assert called == []  # lane off -> no extract call


def test_linkedin_company_lane_no_search_url_no_extract():
    """No LinkedIn company URL surfaced -> the extract seam is never called."""
    called = []

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),  # default results: no linkedin.com/company
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
        linkedin_extract=lambda url: (called.append(url), "content")[1],
    )
    researcher.research("john@acme.com", "acme.com")
    assert called == []


def test_guard_demotes_bare_root_verified_fact():
    """Exact-page guard on the company path: a 'verified' fact citing only the
    bare domain root is demoted to 'unverified' with the honest source_note."""
    response = {
        "company_name": "Acme Construction",
        "industry": "General Contractor",
        "location": "Dallas, TX",
        "website": "https://acme.com",
        "facts": [
            {"claim": "Full service general contractor", "source_url": "https://acme.com", "source_type": "website", "confidence": "verified"},
            {"claim": "Founded in 1990", "source_url": "https://acme.com/about", "source_type": "about_page", "confidence": "verified"},
        ],
    }
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.facts[0].confidence == "unverified"
    assert profile.facts[0].source_note == "source location not reported"
    assert profile.facts[1].confidence == "verified"  # exact /about page survives


def test_research_deep_demotes_bare_root_verified_fact():
    """The exact-page guard also applies to deep-research growth facts."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai({
            "facts": [
                {"claim": "Expanding", "source_url": "https://acme.com", "source_type": "website", "confidence": "verified"},
                {"claim": "Won bid", "source_url": "https://news.com/award-2026", "source_type": "news", "confidence": "verified"},
            ]
        }),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    facts = researcher.research_deep("acme.com", "Acme Construction")
    assert facts[0].confidence == "unverified"
    assert facts[1].confidence == "verified"


def test_source_note_roundtrip_from_ai():
    """source_note from the AI response survives into the parsed fact."""
    response = {
        "company_name": "Acme",
        "industry": "General Contractor",
        "location": "",
        "website": "https://acme.com",
        "facts": [
            {"claim": "Offices in Austin", "source_url": "https://acme.com/contact", "source_type": "contact_page", "confidence": "verified", "source_note": "Contact page"},
        ],
    }
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(response),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.facts[0].source_note == "Contact page"
    assert profile.facts[0].confidence == "verified"
