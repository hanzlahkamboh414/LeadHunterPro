"""Offline tests for Increment7 — LeadPipeline orchestration.

Assembles gate-ACCEPTED companies into qualified Leads through injected
stubs for every network stage (leadership, intent plugins, scorer) — no
live network (CLAUDE.md §1). Pins honest outcomes: the deterministic gate
reports EXACTLY what is missing, a partial lead is surfaced (never silently
passed), and a failing stage never kills the assembly.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.crawlers.html_parser import HTMLParser
from app.discovery.intent.company_site import CompanySiteIntentPlugin
from app.discovery.sources.status import SourceStatus
from app.engines.discovery.company.company_models import CompanyDiscoveryResult
from app.engines.lead.lead_models import IntentEvidence, IntentEvidenceType
from app.engines.lead.lead_pipeline import LeadPipeline, lead_to_export_row
from tests.fixtures.intent_pages import HIRING_CAREERS_HTML, PROJECT_HOME_HTML

BASE = "https://texasskylineco.com"


# -- build blocks --------------------------------------------------------

def _company(*, accepted: bool = True) -> CompanyDiscoveryResult:
    return CompanyDiscoveryResult(
        company_name="Texas Skyline Roofing",
        website=BASE,
        city="Dallas",
        state="TX",
        source="directory",
        source_url="https://directory.example/texasskylineco",
        metadata={"gate_accepted": accepted} if accepted else {},
    )


def _person(name: str = "Maria Gomez", role: str = "Owner", relevant: bool = True) -> dict:
    """A leadership record in the ``PersonRecord.to_dict()`` shape."""
    return {
        "person": {
            "name": name,
            "role": role,
            "role_relevance": relevant,
            "tier": "unverified",
            "source_url": f"{BASE}/team",
        },
        "emails": [],
    }


def _person_with_email(person: dict, email: str, tier: str = "person_bound") -> dict:
    return {
        **person,
        "emails": [{"email": email, "tier": tier, "source_url": f"{BASE}/team"}],
    }


def _bid_evidence() -> IntentEvidence:
    return IntentEvidence(
        type=IntentEvidenceType.bid_award,
        source_url="https://www.usaspending.gov/award/ABC123",
        snippet="Award for roofing services",
        date="2026-06-01",
        source="usaspending",
    )


def _qualifying_result() -> dict:
    return {
        "score": 95,
        "deterministic_score": 60,
        "ai_score": 95,
        "ai_confidence": 95,
        "justification": "USASpending award ABC123 proves an active buying window",
        "qualified": True,
        "qualification": "strong fit",
        "reasons": ["bid award evidence"],
        "strengths": [],
        "concerns": [],
        "ai_used": True,
        "error": None,
    }


# -- network stubs --------------------------------------------------------

class _FakeLeadership:
    def __init__(self, records=None, error=None):
        self.records = records if records is not None else []
        self.error = error
        self.called_with: str | None = None

    def discover(self, website):
        self.called_with = website
        if self.error is not None:
            raise self.error
        return self.records


class _FakeIntentPlugin:
    name = "fake_intent"

    def __init__(self, evidence=None, error=None):
        self.evidence = evidence if evidence is not None else []
        self.error = error
        self.calls: list[tuple] = []

    def collect_evidence(self, *, company_name, website="", location=""):
        self.calls.append((company_name, website, location))
        if self.error is not None:
            raise self.error
        if not self.evidence:
            return SourceStatus.EMPTY, [], {}
        return SourceStatus.SUCCESS, self.evidence, {}


class _FakeScorer:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else _qualifying_result()
        self.error = error
        self.called: tuple | None = None

    def qualify(self, company, query=None):
        self.called = (company, query)
        if self.error is not None:
            raise self.error
        return self.result


def _qualified_records() -> list[dict]:
    return [_person_with_email(_person(), "m.gomez@texasskylineco.com")]


def _pipeline(*, leadership=None, plugins=None, scorer=None) -> LeadPipeline:
    return LeadPipeline(
        leadership=(
            leadership if leadership is not None else _FakeLeadership(_qualified_records())
        ),
        intent_plugins=(
            plugins if plugins is not None else [_FakeIntentPlugin(evidence=[_bid_evidence()])]
        ),
        scorer=scorer if scorer is not None else _FakeScorer(),
    )


class TestHappyPath:

    def test_verified_company_with_all_signals_qualifies(self):
        lead = _pipeline().qualify_company(_company())

        assert lead.qualifies is True
        assert lead.person is not None
        assert lead.person.name == "Maria Gomez"
        assert lead.person.role_relevance is True
        assert lead.has_person_bound_email
        assert lead.ai.ai_confidence == 95
        assert lead.ai.justification
        assert lead.qualification_gate().blocked_by == []


class TestDecisionMakerSelection:

    def test_prefers_relevant_role_over_irrelevant(self):
        records = [
            _person_with_email(
                _person(name="Dana Smith", role="Marketing Coordinator", relevant=False),
                "d.smith@texasskylineco.com",
            ),
            _person_with_email(_person(), "m.gomez@texasskylineco.com"),
        ]

        lead = _pipeline(leadership=_FakeLeadership(records)).qualify_company(_company())

        assert lead.person is not None
        assert lead.person.name == "Maria Gomez"
        assert lead.person.role_relevance is True
        assert lead.qualifies is True

    def test_among_relevant_prefers_person_bound_email(self):
        records = [
            _person_with_email(
                _person(name="Adeel Khan", role="Project Manager"),
                "office@texasskylineco.com",
                tier="format",
            ),
            _person_with_email(_person(), "m.gomez@texasskylineco.com"),
        ]

        lead = _pipeline(leadership=_FakeLeadership(records)).qualify_company(_company())

        assert lead.person.name == "Maria Gomez"
        assert lead.has_person_bound_email

    def test_relevant_person_with_only_generic_email_blocked(self):
        records = [
            _person_with_email(_person(), "info@texasskylineco.com", tier="format")
        ]

        lead = _pipeline(leadership=_FakeLeadership(records)).qualify_company(_company())

        assert lead.qualifies is False
        assert any("person_bound" in b for b in lead.qualification_gate().blocked_by)


class TestPlanHolderBridge:
    """Inc 3: the pre-bound plan-holder person/email is used WITHOUT a site crawl.

    A plan-holder record carries ``metadata["plan_holder"]`` (Inc 2) — the
    named contact + person-bound email from the public bid list. The pipeline
    must bridge that into the Lead instead of re-crawling the record's
    derived, unverified website. Role is never backfilled (hard rule #2).
    """

    def _plan_holder_company(self, *, accepted: bool = False) -> CompanyDiscoveryResult:
        """A plan-holder company: unverified, with the Inc-2 detail block."""
        return CompanyDiscoveryResult(
            company_name="Pirc-Tobin",
            website="https://pirctobin.com",  # derived from email domain, not verified
            city="",
            state="",
            source="texas_procurement",
            source_url="https://www.hrgreen.com/.../Plan-Holder-List_20250121.pdf",
            metadata={
                "gate_accepted": accepted,
                "verification_status": "verified" if accepted else "unknown",
                "plan_holder": {
                    "person": {
                        "name": "Charlie Arnold",
                        "role": "",  # plan-holder list carries no title
                        "role_relevance": False,  # never invented (hard rule #2)
                        "tier": "unverified",
                        "source_url": "https://www.hrgreen.com/.../Plan-Holder-List_20250121.pdf",
                    },
                    "emails": [
                        {
                            "email": "cjarnold@pirctobin.com",
                            "tier": "person_bound",  # explicit row binding (hard rule #3)
                            "source_url": "https://www.hrgreen.com/.../Plan-Holder-List_20250121.pdf",
                        }
                    ],
                    "phones": [],
                    "domain": "pirctobin.com",
                    "free_mail_only": False,
                },
            },
        )

    def test_bridge_uses_prebound_person_without_crawl(self):
        leadership = _FakeLeadership([])  # would return nobody
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )

        # Inc 4: the website IS crawled for role enrichment, but the
        # pre-bound person/email from the plan-holder block are used
        # (not replaced by whatever the crawl returns).
        assert leadership.called_with == "https://pirctobin.com"
        assert lead.person is not None
        assert lead.person.name == "Charlie Arnold"
        assert lead.person.role == ""  # crawl returned nobody → role stays empty
        assert lead.person.role_relevance is False
        assert lead.has_person_bound_email
        assert lead.emails[0].email == "cjarnold@pirctobin.com"
        assert lead.emails[0].tier.value == "person_bound"

    def test_bridge_person_bound_email_satisfies_email_rule(self):
        """The person-bound email is real, so the email hard rule is met."""
        lead = _pipeline().qualify_company(self._plan_holder_company())
        assert lead.has_person_bound_email
        assert not any(
            "person_bound" in b for b in lead.qualification_gate().blocked_by
        )

    def test_bridge_does_not_fake_verification_or_role(self):
        """The V1 gate still blocks on company-not-verified.

        When the website crawl returns no relevant role, role_relevance
        stays False and is also a blocker. When the crawl DOES find a
        relevant role (Inc 4 enrichment), the role blocker is gone but
        'company not verified' remains — the gate is never weakened.
        """
        leadership = _FakeLeadership([])  # empty → no role enrichment
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )
        blockers = lead.qualification_gate().blocked_by
        assert any("company not verified" in b for b in blockers)
        assert any("role_relevance" in b for b in blockers)
        assert lead.qualifies is False

    def test_no_plan_holder_block_falls_through_to_leadership(self):
        """A normal company (no plan_holder) still uses website leadership."""
        records = [_person_with_email(_person(), "m.gomez@texasskylineco.com")]
        leadership = _FakeLeadership(records)
        lead = _pipeline(leadership=leadership).qualify_company(_company())
        assert leadership.called_with == BASE
        assert lead.person.name == "Maria Gomez"

    def test_export_row_flags_unverified_plan_holder(self):
        """The bridged unverified row is unmistakably NOT ready to contact.

        decision_maker + person_bound_email are populated (the bridge), but
        gate_accepted False + verification_status "unknown" must make it
        visually/programmatically distinct from a fully-qualified lead.
        """
        lead = _pipeline().qualify_company(self._plan_holder_company())
        row = lead_to_export_row(lead)

        assert row["decision_maker"] == "Charlie Arnold"  # bridged person
        assert row["person_bound_email"] == "cjarnold@pirctobin.com"  # bridged email
        assert row["gate_accepted"] is False
        assert row["verification_status"] == "unknown"  # NOT ready to contact
        assert row["qualified"] is False
        assert any("not verified" in b for b in row["blocked_by"])

    # -- Inc 4: role enrichment via website crawl ---------------------------

    def test_role_enrichment_updates_role_from_website(self):
        """The derived website yields a relevant role → role + role_relevance update."""
        enriched_records = [
            _person_with_email(
                _person(name="Charlie Arnold", role="Project Manager", relevant=True),
                "cjarnold@pirctobin.com",
            )
        ]
        leadership = _FakeLeadership(enriched_records)
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )
        # Role enriched from website crawl
        assert lead.person.role == "Project Manager"
        assert lead.person.role_relevance is True
        # Pre-bound name and email preserved
        assert lead.person.name == "Charlie Arnold"
        assert lead.emails[0].email == "cjarnold@pirctobin.com"

    def test_role_enrichment_crawl_failure_leaves_person_unchanged(self):
        """A failed website crawl leaves role="" and role_relevance=False."""
        leadership = _FakeLeadership(error=RuntimeError("connection refused"))
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )
        assert lead.person.name == "Charlie Arnold"
        assert lead.person.role == ""
        assert lead.person.role_relevance is False
        assert lead.emails[0].email == "cjarnold@pirctobin.com"

    def test_role_enrichment_empty_result_leaves_person_unchanged(self):
        """An empty website crawl (no people found) leaves role unchanged."""
        leadership = _FakeLeadership([])  # no records
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )
        assert lead.person.role == ""
        assert lead.person.role_relevance is False

    def test_role_enrichment_irrelevant_role_not_applied(self):
        """A role found on the website but not plausibly relevant is not applied."""
        irrelevant_records = [
            _person_with_email(
                _person(name="Charlie Arnold", role="Marketing Coordinator", relevant=False),
                "cjarnold@pirctobin.com",
            )
        ]
        leadership = _FakeLeadership(irrelevant_records)
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )
        # Irrelevant role NOT applied — stays as plan-holder default
        assert lead.person.role == ""
        assert lead.person.role_relevance is False

    def test_role_enrichment_preserves_verification_status(self):
        """Role enrichment never changes verification_status or gate_accepted."""
        enriched_records = [
            _person_with_email(
                _person(name="Charlie Arnold", role="Owner", relevant=True),
                "cjarnold@pirctobin.com",
            )
        ]
        leadership = _FakeLeadership(enriched_records)
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )
        # verification_status and gate unchanged despite role enrichment
        assert lead.company.metadata.get("verification_status") == "unknown"
        assert lead.company.metadata.get("gate_accepted") is False
        assert lead.verified_context is False

    def test_role_enrichment_preserves_person_bound_email(self):
        """The pre-bound email is never replaced or regenerated."""
        enriched_records = [
            _person_with_email(
                _person(name="Charlie Arnold", role="Estimator", relevant=True),
                "different@email.com",  # different email from crawl
            )
        ]
        leadership = _FakeLeadership(enriched_records)
        lead = _pipeline(leadership=leadership).qualify_company(
            self._plan_holder_company()
        )
        # Original person-bound email preserved, not replaced by crawl result
        assert lead.emails[0].email == "cjarnold@pirctobin.com"

    def test_role_enrichment_skips_already_relevant_role(self):
        """If the plan-holder already has a relevant role, no crawl is done."""
        company = self._plan_holder_company()
        # Override plan_holder person to already have a relevant role
        company.metadata["plan_holder"]["person"]["role"] = "Owner"
        company.metadata["plan_holder"]["person"]["role_relevance"] = True
        leadership = _FakeLeadership([])  # would return nobody
        lead = _pipeline(leadership=leadership).qualify_company(company)
        # No crawl needed — role already relevant
        assert leadership.called_with is None
        assert lead.person.role == "Owner"
        assert lead.person.role_relevance is True


class TestHonestBlockers:

    def test_unverified_company_blocked_despite_all_signals(self):
        lead = _pipeline().qualify_company(_company(accepted=False))

        assert lead.qualifies is False
        assert any("not verified" in b for b in lead.qualification_gate().blocked_by)

    def test_no_person_reports_missing_decision_maker(self):
        lead = _pipeline(leadership=_FakeLeadership([])).qualify_company(_company())

        assert lead.person is None
        assert lead.qualifies is False
        assert any("decision-maker" in b for b in lead.qualification_gate().blocked_by)

    def test_no_intent_evidence_blocked(self):
        lead = _pipeline(plugins=[_FakeIntentPlugin()]).qualify_company(_company())

        assert lead.qualifies is False
        assert any("buying-intent evidence" in b for b in lead.qualification_gate().blocked_by)

    def test_low_confidence_blocked(self):
        result = _qualifying_result()
        result["ai_confidence"] = 40

        lead = _pipeline(scorer=_FakeScorer(result=result)).qualify_company(_company())

        assert lead.qualifies is False
        assert any("ai_confidence" in b for b in lead.qualification_gate().blocked_by)

    def test_missing_justification_blocked(self):
        result = _qualifying_result()
        result["justification"] = ""

        lead = _pipeline(scorer=_FakeScorer(result=result)).qualify_company(_company())

        assert lead.qualifies is False
        assert any("justification" in b for b in lead.qualification_gate().blocked_by)


class TestStageFailureNeverFatal:

    def test_leadership_failure_yields_no_person_no_crash(self):
        lead = _pipeline(leadership=_FakeLeadership(error=RuntimeError("down"))).qualify_company(
            _company()
        )

        assert lead.person is None
        assert lead.qualifies is False

    def test_plugin_failure_still_collects_from_healthy_plugins(self):
        plugins = [
            _FakeIntentPlugin(error=RuntimeError("boom")),
            _FakeIntentPlugin(evidence=[_bid_evidence()]),
        ]

        lead = _pipeline(plugins=plugins).qualify_company(_company())

        assert len(lead.intent_evidence) == 1
        assert lead.qualifies is True

    def test_scorer_failure_sets_error_honestly(self):
        lead = _pipeline(scorer=_FakeScorer(error=RuntimeError("down"))).qualify_company(
            _company()
        )

        assert "RuntimeError" in (lead.ai.error or "")
        assert lead.ai.ai_confidence == 0.0
        assert lead.qualifies is False
        assert any("ai_confidence" in b for b in lead.qualification_gate().blocked_by)


class TestScorerContract:

    def test_intent_evidence_reaches_scorer(self):
        scorer = _FakeScorer()

        _pipeline(scorer=scorer).qualify_company(_company())

        assert scorer.called is not None
        company_data, _query = scorer.called
        assert company_data["company_name"] == "Texas Skyline Roofing"
        urls = [e["source_url"] for e in company_data["intent_evidence"]]
        assert "https://www.usaspending.gov/award/ABC123" in urls

    def test_query_location_forwarded_to_plugins(self):
        plugin = _FakeIntentPlugin(evidence=[_bid_evidence()])

        _pipeline(plugins=[plugin]).qualify_company(
            _company(), query={"industry": "Roofing", "location": "Dallas Texas"}
        )

        _company_name, website, location = plugin.calls[0]
        assert website == BASE
        assert location == "Dallas Texas"

    def test_duplicate_evidence_deduped_by_type_and_url(self):
        lead = _pipeline(
            plugins=[_FakeIntentPlugin(evidence=[_bid_evidence(), _bid_evidence()])]
        ).qualify_company(_company())

        assert len(lead.intent_evidence) == 1


class TestBatchAndExport:

    def test_qualify_many_preserves_order(self):
        companies = [_company(), _company(accepted=False)]

        leads = _pipeline().qualify_many(companies)

        assert [l.qualifies for l in leads] == [True, False]

    def test_export_row_reuses_demo_contract(self):
        lead = _pipeline().qualify_company(_company())

        row = lead_to_export_row(lead)

        for key in (
            "company_name",
            "website",
            "city",
            "state",
            "industry",
            "source",
            "source_url",
            "gate_accepted",
            "verification_status",
            "decision_maker",
            "decision_maker_role",
            "person_bound_email",
            "intent_evidence",
            "ai_confidence",
            "justification",
            "deterministic_score",
            "ai_score",
            "ai_used",
            "qualified",
            "blocked_by",
            "created_at",
        ):
            assert key in row, key
        assert row["qualified"] is True
        assert row["gate_accepted"] is True
        assert row["decision_maker"] == "Maria Gomez"
        assert row["person_bound_email"] == "m.gomez@texasskylineco.com"


class TestRealPluginSeam:

    def test_real_company_site_plugin_wires_into_pipeline(self):
        """The real plugin + saved-HTML fixtures prove the intent seam e2e."""

        class _FakeFetcher:
            def __init__(self, pages):
                self.pages = pages

            def fetch(self, url):
                html = self.pages.get(urlsplit(url).path or "/")
                if html is None:
                    return None
                return HTMLParser().parse(html, base_url=url)

        plugin = CompanySiteIntentPlugin(
            page_fetcher=_FakeFetcher(
                {"/": PROJECT_HOME_HTML, "/careers": HIRING_CAREERS_HTML}
            )
        )

        lead = LeadPipeline(
            leadership=_FakeLeadership(_qualified_records()),
            intent_plugins=[plugin],
            scorer=_FakeScorer(),
        ).qualify_company(_company())

        kinds = {e.type for e in lead.intent_evidence}
        assert kinds == {IntentEvidenceType.project, IntentEvidenceType.hiring}
        assert lead.qualifies is True
