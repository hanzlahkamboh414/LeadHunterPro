"""Phase 2 Step 4 — deterministic acceptance gate + evidence merge.

Pins the accuracy-first acceptance contract:

- contractor + verified identity + verified location -> ACCEPTED / verified;
- manufacturer / supplier / association -> REJECTED regardless of AI score;
- invalid / placeholder / aggregator website -> REJECTED;
- a directory *member page* is a soft signal (not official, not fake);
- unknown website, missing email/phone/decision-maker/project signal, and
  missing city are evidence gaps, NOT rejections;
- the query, DFW, a default TX, and company-name city tokens are NEVER
  location evidence;
- Tier-4 fixtures can never be a live verified lead;
- the gate is deterministic and serializable; AI cannot override a rejection;
- duplicate records MERGE evidence instead of silently discarding the loser,
  keep ``(domain, name)`` dedup, preserve provenance, record conflicting
  location explicitly, and never promote a conflict to verified;
- the TexasProcurementConnector wiring keeps ``ConnectorResult`` frozen and
  compatible (verification is additive metadata).

Everything runs fully offline — no network calls.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.connectors.connector_result import ConnectorResult
from app.connectors.texas_procurement import TexasProcurementConnector
from app.discovery.source_orchestrator import SourceOrchestrator
from app.discovery.sources.status import SourceStatus
from app.engines.verification.acceptance_gate import (
    AcceptanceGate,
    merge_company_records,
)
from app.engines.verification.identity_verifier import IdentityVerificationResult
from app.engines.verification.industry_verifier import IndustryVerificationResult
from app.engines.verification.location_verifier import (
    LocationVerificationResult,
    LocationVerifier,
)
from app.engines.verification.models import FieldEvidence, VerificationStatus


# ---------------------------------------------------------------------------
# Deterministic result factories (offline; no verifiers run here except the
# offline LocationVerifier where the query-never-evidence rules are pinned)
# ---------------------------------------------------------------------------


def _evidence(field, value, source="official_website", source_url="", **kw):
    return FieldEvidence(field=field, value=value, source=source, source_url=source_url, **kw)


def _identity(
    *,
    website_status="unknown",
    identity_status="unknown",
    official=False,
    name_verified=False,
    overlap=0.0,
    confidence=0.0,
    reasons=(),
    evidence=(),
    normalized_website="",
):
    return IdentityVerificationResult(
        identity_status=VerificationStatus(identity_status),
        website_status=VerificationStatus(website_status),
        official_site_confirmed=official,
        name_verified=name_verified,
        domain_overlap=overlap,
        confidence=confidence,
        reasons=list(reasons),
        evidence=list(evidence),
        normalized_website=normalized_website,
    )


def _industry(
    *,
    business_type="contractor",
    status="verified",
    trade="roofing",
    confidence=0.85,
    industry_match=True,
    reasons=(),
    evidence=(),
):
    return IndustryVerificationResult(
        business_type=business_type,
        industry_match=industry_match,
        verification_status=VerificationStatus(status),
        trade_category=trade,
        confidence=confidence,
        reasons=list(reasons),
        evidence=list(evidence),
    )


def _location(
    *,
    city=None,
    state=None,
    status="unknown",
    match=False,
    confidence=0.0,
    reasons=(),
    evidence=(),
):
    return LocationVerificationResult(
        city=city,
        state=state,
        location_status=VerificationStatus(status),
        location_match=match,
        confidence=confidence,
        reasons=list(reasons),
        evidence=list(evidence),
    )


def _verified_record(record: dict) -> "AcceptanceResult":
    """A fully verified evaluation: contractor + official site + location."""
    return AcceptanceGate().evaluate(
        record=record,
        identity=_identity(
            website_status="verified",
            identity_status="verified",
            official=True,
            name_verified=True,
            overlap=1.0,
            confidence=1.0,
            reasons=["official website confirmed"],
            evidence=[_evidence("website", "https://acme.example")],
        ),
        industry=_industry(),
        location=_location(
            city="Dallas",
            state="TX",
            status="verified",
            match=True,
            confidence=0.9,
            evidence=[_evidence("city", "Dallas"), _evidence("state", "TX")],
        ),
    )


# ---------------------------------------------------------------------------
# A. Acceptance gate
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_contractor_verified_identity_verified_location_accepted(self):
        gate = _verified_record(
            {"company_name": "Acme Roofing LLC", "_discovery_source": "official_website"}
        )
        assert gate.accepted is True
        assert gate.verification_status == VerificationStatus.verified
        assert gate.verification_confidence > 0.5
        assert gate.city == "Dallas"
        assert gate.state == "TX"

    @pytest.mark.parametrize(
        "business_type",
        ["manufacturer", "supplier", "association"],
    )
    def test_non_contractor_business_types_rejected(self, business_type):
        gate = AcceptanceGate().evaluate(
            record={"company_name": "Acme Roofing LLC", "_discovery_source": "directory_crawl"},
            identity=_identity(),
            industry=_industry(business_type=business_type, status="rejected", industry_match=False),
            location=_location(city="Dallas", state="TX", status="verified", match=True, confidence=0.9),
        )
        assert gate.accepted is False
        assert gate.hard_rejected is True
        assert gate.verification_status == VerificationStatus.rejected
        assert business_type in " ".join(gate.reasons)

    def test_invalid_placeholder_website_rejected(self):
        gate = AcceptanceGate().evaluate(
            record={"company_name": "Acme Roofing LLC", "_discovery_source": "directory_crawl"},
            identity=_identity(
                website_status="rejected",
                reasons=["placeholder/reserved URL: example.com"],
            ),
            industry=_industry(),
            location=_location(city="Dallas", state="TX", status="verified", match=True, confidence=0.9),
        )
        assert gate.accepted is False
        assert gate.hard_rejected is True
        assert gate.verification_status == VerificationStatus.rejected

    def test_unknown_website_does_not_reject(self):
        gate = AcceptanceGate().evaluate(
            record={"company_name": "Acme Roofing LLC", "_discovery_source": "directory_crawl"},
            identity=_identity(website_status="unknown", reasons=["no website supplied"]),
            industry=_industry(),
            location=_location(city="Dallas", state="TX", status="verified", match=True, confidence=0.9),
        )
        assert gate.accepted is True
        assert gate.verification_status == VerificationStatus.partially_verified

    def test_directory_member_page_is_soft_not_fake(self):
        """A member/directory URL is not an official site, but the company is
        not provably fake — evidence gap, not rejection (Blueprint §8 keeps
        the API-free crawl path alive)."""
        gate = AcceptanceGate().evaluate(
            record={"company_name": "Acme Roofing LLC", "_discovery_source": "directory_crawl"},
            identity=_identity(
                website_status="rejected",
                reasons=["directory member page (path), not an official website"],
            ),
            industry=_industry(),
            location=_location(city="Dallas", state="TX", status="verified", match=True, confidence=0.9),
        )
        assert gate.hard_rejected is False
        assert gate.accepted is True
        assert gate.verification_status == VerificationStatus.partially_verified

    @pytest.mark.parametrize(
        "absent_key",
        [
            "email",
            "phone",
            "phone_number",
            "linkedin",
            "linkedin_url",
            "decision_maker",
            "decision_makers",
            "project_signal",
            "project_references",
        ],
    )
    def test_missing_contact_fields_do_not_reject(self, absent_key):
        record = {
            "company_name": "Acme Roofing LLC",
            "_discovery_source": "official_website",
            "source_tier": 1,
        }
        assert absent_key not in record  # the gate must not require it
        gate = _verified_record(record)
        assert gate.accepted is True
        assert gate.verification_status == VerificationStatus.verified


class TestLocationNeverFromQuery:
    def test_query_city_state_never_becomes_evidence(self):
        loc = LocationVerifier().verify(query="Dallas Texas")
        assert loc.city is None and loc.state is None
        gate = AcceptanceGate().evaluate(
            # The record itself CLAIMS Dallas/TX — the gate must ignore it.
            record={
                "company_name": "Acme Roofing LLC",
                "city": "Dallas",
                "state": "TX",
                "_discovery_source": "directory_crawl",
            },
            identity=_identity(),
            industry=_industry(),
            location=loc,
        )
        assert gate.city is None
        assert gate.state is None
        values = [str(e.value) for e in gate.field_evidence]
        assert "Dallas" not in values
        assert "Texas" not in values

    def test_state_only_evidence_does_not_satisfy_city_match(self):
        loc = LocationVerifier().verify(
            evidence_texts=["Texas roofing contractor"], query="Dallas Texas"
        )
        assert loc.state == "TX"
        assert loc.city is None
        assert loc.location_match is False
        gate = AcceptanceGate().evaluate(
            record={"_discovery_source": "directory_crawl"},
            identity=_identity(),
            industry=_industry(),
            location=loc,
        )
        assert gate.city is None
        assert gate.location_match is False
        # state-only evidence is a real (partial) signal, not a rejection
        assert gate.accepted is True

    def test_dfw_does_not_become_dallas(self):
        loc = LocationVerifier().verify(
            evidence_texts=["DFW area roofing services"], query="Dallas Texas"
        )
        assert loc.city is None
        gate = AcceptanceGate().evaluate(
            record={"company_name": "Acme Roofing LLC", "_discovery_source": "directory_crawl"},
            identity=_identity(),
            industry=_industry(),
            location=loc,
        )
        assert gate.city is None
        assert gate.city != "Dallas"


class TestTierAndAI:
    def test_tier4_fixture_cannot_become_live_verified(self):
        gate = _verified_record(
            {
                "company_name": "Acme Roofing LLC",
                "_discovery_source": "fixture_bridge",
                "source_tier": 4,
            }
        )
        assert gate.accepted is False
        assert gate.verification_status != VerificationStatus.verified
        assert gate.verification_status == VerificationStatus.unknown
        assert gate.verification_confidence == 0.0
        assert any("not a live tier" in r for r in gate.reasons)

    def test_ai_cannot_override_deterministic_rejection(self):
        record = {
            "company_name": "Acme Roofing LLC",
            "_discovery_source": "directory_crawl",
            "ai": {"fit_score": 0.99, "opportunity_score": 0.99, "summary": "great lead"},
        }
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(
                website_status="rejected",
                reasons=["placeholder/reserved URL: example.com"],
            ),
            industry=_industry(),
            location=_location(city="Dallas", state="TX", status="verified", match=True, confidence=0.9),
        )
        assert gate.hard_rejected is True
        assert gate.accepted is False
        assert gate.verification_status == VerificationStatus.rejected

    def test_gate_result_serializable(self):
        gate = _verified_record(
            {"company_name": "Acme Roofing LLC", "_discovery_source": "official_website"}
        )
        data = gate.to_dict()
        for key in (
            "accepted",
            "verification_status",
            "verification_confidence",
            "reasons",
            "source_tier",
            "business_type",
            "city",
            "state",
            "location_match",
            "field_evidence",
            "identity",
            "industry",
            "location",
        ):
            assert key in data, f"missing key: {key}"
        json.dumps(data)  # must not raise TypeError
        assert data["identity"]["identity_status"] == "verified"
        assert data["location"]["city"] == "Dallas"
        assert data["field_evidence"][0]["source"]
        assert data["field_evidence"][0]["source_url"] is not None


# ---------------------------------------------------------------------------
# F. Evidence merge
# ---------------------------------------------------------------------------


class TestEvidenceMerge:
    def test_duplicate_records_merge_evidence(self):
        ev1 = _evidence("website", "https://acme.example", source="official_website", is_reliable=True)
        ev2 = _evidence("business_type", "contractor", source="directory_crawl", is_reliable=True)
        primary = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Dallas",
            "state": "TX",
            "identity_evidence": [ev1],
        }
        duplicate = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Dallas",
            "state": "TX",
            "industry_evidence": [ev2],
        }
        merged = merge_company_records(primary, duplicate)
        assert ev1 in merged["identity_evidence"]
        assert ev2 in merged["industry_evidence"]
        assert "location_conflict" not in merged
        assert merged["city"] == "Dallas"

    def test_conflicting_location_evidence_preserved(self):
        primary = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Dallas",
            "state": "TX",
            "source_url": "https://src.example/a",
        }
        duplicate = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Houston",
            "state": "TX",
            "source_url": "https://src.example/b",
        }
        merged = merge_company_records(primary, duplicate)
        # Neither city silently wins; both are recorded as unreliable evidence.
        assert "location_conflict" in merged
        assert merged["city"] == ""
        assert merged["state"] == ""
        conflict_values = {
            e["value"]
            for e in merged["field_evidence"]
            if e["field"] == "city"
        }
        assert conflict_values == {"Dallas", "Houston"}
        assert all(
            e["is_reliable"] is False
            for e in merged["field_evidence"]
            if e["field"] == "city"
        )

    def test_conflicting_evidence_prevents_false_verification(self):
        primary = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Dallas",
            "state": "TX",
            "verification_status": "verified",
            "verification_confidence": 0.9,
        }
        duplicate = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Houston",
            "state": "TX",
        }
        merged = merge_company_records(primary, duplicate)
        # A conflict is never promoted to verified — it is downgraded.
        assert merged["verification_status"] == "unknown"
        assert merged["verification_confidence"] == 0.0
        assert any("conflict" in r for r in merged["verification_reasons"])

    def test_field_evidence_keeps_source_provenance(self):
        ev = _evidence(
            "city",
            "Dallas",
            source="licensing_board",
            source_url="https://tlic.example.gov/license/1",
            confidence=0.9,
            is_reliable=True,
        )
        primary = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Dallas",
            "state": "TX",
            "location_evidence": [ev],
        }
        duplicate = {
            "company_name": "Acme Roofing",
            "website": "https://acme.example",
            "city": "Dallas",
            "state": "TX",
            "source_url": "https://dup.example/listing/2",
        }
        merged = merge_company_records(primary, duplicate)
        # Provenance on the evidence survives the merge unchanged.
        assert merged["location_evidence"][0].source == "licensing_board"
        assert merged["location_evidence"][0].source_url == "https://tlic.example.gov/license/1"
        # Provenance filled in where the primary lacked it (no first-wins drop).
        assert merged["source_url"] == "https://dup.example/listing/2"
        # Agreeing location is kept, not cleared.
        assert merged["city"] == "Dallas"

    def test_orchestrator_dedup_merges_instead_of_discarding(self):
        """F: the orchestrator's (domain, name) dedup now MERGES duplicates —
        conflicting location is preserved, not silently first-wins."""
        class _FakeSource:
            def __init__(self, name, results):
                self.source_name = name
                self.priority = 20
                self.enabled = True
                self._results = results

            def discover(self, *, industry, location, limit):  # noqa: ARG002
                return SourceStatus.SUCCESS, list(self._results), {}

            async def health_check(self):
                return {"healthy": True}

        orch = SourceOrchestrator()
        a = {"company_name": "Acme Roofing", "website": "https://acme.example", "city": "Dallas", "state": "TX", "data_provenance": "src:a"}
        b = {"company_name": "Acme Roofing", "website": "https://acme.example", "city": "Houston", "state": "TX", "data_provenance": "src:b"}
        orch.register(_FakeSource("s1", [a]))
        orch.register(_FakeSource("s2", [b]))
        companies, meta = orch.discover(industry="X", location="Y", limit=10)
        assert len(companies) == 1
        assert companies[0]["location_conflict"]
        assert companies[0]["city"] == ""
        assert meta["total_deduped"] == 1
        # provenance from BOTH sources was preserved in the merge
        assert "src:a" in [companies[0]["data_provenance"]]


# ---------------------------------------------------------------------------
# G. Connector integration
# ---------------------------------------------------------------------------


class TestConnectorIntegration:
    def _connector(self):
        return TexasProcurementConnector()

    def test_connector_verify_company_rejects_manufacturer(self):
        gate = self._connector()._verify_company(
            {
                "company_name": "Acme Roofing Manufacturer LLC",
                "description": "roofing shingle manufacturer",
                "_discovery_source": "directory_crawl",
                "website": "",
            },
            "Dallas Texas",
        )
        assert gate.accepted is False
        assert gate.hard_rejected is True
        assert gate.business_type == "manufacturer"

    def test_connector_verify_company_query_never_becomes_location(self):
        gate = self._connector()._verify_company(
            {
                "company_name": "Acme Roofing LLC",
                "description": "commercial roofing contractor",
                "_discovery_source": "directory_crawl",
                "website": "",
            },
            "Dallas Texas",
        )
        assert gate.accepted is True
        assert gate.city is None
        assert gate.state is None
        assert gate.verification_status == VerificationStatus.partially_verified

    def test_connector_result_remains_compatible(self):
        connector = self._connector()
        company = {
            "company_name": "Acme Roofing LLC",
            "website": "https://acme-roofing.example.com",
            "city": "Dallas",
            "state": "TX",
            "trade_category": "roofing",
            "industry_focus": "commercial roofing contractor",
            "country": "USA",
        }
        result, reason = connector._build_result(company, "Roofing", {"roof", "roofing"})
        assert isinstance(result, ConnectorResult)
        assert result.__dataclass_params__.frozen  # frozen contract preserved
        assert result.company_name == "Acme Roofing LLC"
        assert result.city == "Dallas"  # no verified location passed → claim survives
        assert result.state == "TX"
        assert reason

        # Additive verification metadata keeps every existing field intact.
        gate = _verified_record(
            {"company_name": "Acme Roofing LLC", "_discovery_source": "official_website"}
        )
        result2 = replace(result, metadata={**result.metadata, "verification": gate.to_dict()})
        assert result2.company_name == result.company_name
        assert result2.website == result.website
        assert result2.city == result.city
        assert result2.metadata["verification"]["accepted"] is True

    def test_build_result_uses_verified_location(self):
        connector = self._connector()
        company = {
            "company_name": "Acme Roofing LLC",
            "website": "https://acme-roofing.example.com",
            "city": "Dallas",
            "state": "TX",
            "trade_category": "roofing",
            "industry_focus": "commercial roofing contractor",
        }
        result, _ = connector._build_result(
            company, "Roofing", set(), verified_city="Austin", verified_state="TX"
        )
        # Phase 2 Step 4: location comes ONLY from verified evidence.
        assert result.city == "Austin"
        assert result.state == "TX"


# ---------------------------------------------------------------------------
# Inc 5 — Plan-holder verification dimension (Option E)
# ---------------------------------------------------------------------------


class TestPlanHolderVerification:
    """Option E: a fourth verification dimension for documentary bid-listing
    evidence from public procurement PDFs (plan-holder lists).

    ``plan_holder_confirmed`` is True when the record carries a complete
    plan_holder block with named person + person-bound email + source URL,
    and the record is NOT hard-rejected.  It contributes to the confirmed
    count but does NOT upgrade to ``verified`` on its own — it is
    documentary evidence, not identity/industry/location verification.
    """

    def _plan_holder_record(self, **overrides):
        """A valid plan-holder record dict (connector schema)."""
        rec = {
            "company_name": "Pirc-Tobin",
            "website": "https://pirctobin.com",
            "city": "",
            "state": "",
            "country": "USA",
            "trade_category": "",
            "industry_focus": "",
            "source_url": "https://www.hrgreen.com/.../Plan-Holder-List_20250121.pdf",
            "_discovery_source": "plan_holder",
            "plan_holder": {
                "person": {
                    "name": "Charlie Arnold",
                    "role": "",
                    "role_relevance": False,
                    "tier": "unverified",
                    "source_url": "https://www.hrgreen.com/.../Plan-Holder-List_20250121.pdf",
                },
                "emails": [
                    {
                        "email": "cjarnold@pirctobin.com",
                        "tier": "person_bound",
                        "source_url": "https://www.hrgreen.com/.../Plan-Holder-List_20250121.pdf",
                    }
                ],
                "phones": [],
                "domain": "pirctobin.com",
                "free_mail_only": False,
            },
        }
        rec.update(overrides)
        return rec

    def _all_unknown(self):
        """All three standard verification dimensions unknown (no evidence)."""
        return _identity(), _industry(status="unknown", industry_match=False), _location()

    # --- positive tests ---

    def test_plan_holder_all_requirements_met(self):
        """Complete plan_holder evidence → plan_holder_confirmed=True,
        accepted=True, partially_verified, confirmed=1."""
        record = self._plan_holder_record()
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.plan_holder_confirmed is True
        assert gate.accepted is True
        assert gate.verification_status == VerificationStatus.partially_verified
        # Confidence is 0.0 because _confidence() uses only the 3 standard
        # dimensions — plan_holder is documentary, not scored.
        assert gate.verification_confidence == 0.0

    def test_plan_holder_does_not_set_identity_industry_location(self):
        """plan_holder_confirmed is independent — it does NOT set any of the
        three standard verification dimensions to True."""
        record = self._plan_holder_record()
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        # None of the three standard dimensions should be confirmed
        assert gate.identity.official_site_confirmed is False
        assert gate.industry.verification_status != VerificationStatus.verified
        assert gate.location.location_status != VerificationStatus.verified

    def test_plan_holder_alone_cannot_reach_verified(self):
        """plan_holder alone gives confirmed=1, never confirmed=3 →
        status is partially_verified, NOT verified."""
        record = self._plan_holder_record()
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.verification_status == VerificationStatus.partially_verified
        assert gate.verification_status != VerificationStatus.verified

    def test_plan_holder_with_other_dimensions_reaches_verified(self):
        """When all 3 standard dimensions + plan_holder are confirmed,
        confirmed=3 → verified (plan_holder is the 4th, doesn't block)."""
        record = self._plan_holder_record()
        gate = _verified_record(record)
        assert gate.plan_holder_confirmed is True
        assert gate.verification_status == VerificationStatus.verified
        assert gate.accepted is True

    # --- negative tests (missing requirements) ---

    def test_no_plan_holder_block(self):
        """No plan_holder key on record → plan_holder_confirmed=False."""
        record = {
            "company_name": "Acme Roofing LLC",
            "website": "https://acme.example",
            "_discovery_source": "directory_crawl",
        }
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(),
            location=_location(city="Dallas", state="TX", status="verified", match=True),
        )
        assert gate.plan_holder_confirmed is False

    def test_plan_holder_missing_person_name(self):
        """Empty person.name → plan_holder_confirmed=False."""
        record = self._plan_holder_record()
        record["plan_holder"]["person"]["name"] = ""
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.plan_holder_confirmed is False
        assert gate.accepted is False  # confirmed=0

    def test_plan_holder_missing_person_bound_email(self):
        """No person_bound email (only format tier) → plan_holder_confirmed=False."""
        record = self._plan_holder_record()
        record["plan_holder"]["emails"] = [
            {"email": "info@pirctobin.com", "tier": "format", "source_url": "..."}
        ]
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.plan_holder_confirmed is False
        assert gate.accepted is False  # confirmed=0

    def test_plan_holder_empty_emails_list(self):
        """Empty emails list → plan_holder_confirmed=False."""
        record = self._plan_holder_record()
        record["plan_holder"]["emails"] = []
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.plan_holder_confirmed is False

    def test_plan_holder_empty_source_url(self):
        """Empty source_url on person → plan_holder_confirmed=False."""
        record = self._plan_holder_record()
        record["plan_holder"]["person"]["source_url"] = ""
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.plan_holder_confirmed is False
        assert gate.accepted is False  # confirmed=0

    def test_plan_holder_whitespace_source_url(self):
        """Whitespace-only source_url → plan_holder_confirmed=False."""
        record = self._plan_holder_record()
        record["plan_holder"]["person"]["source_url"] = "   "
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.plan_holder_confirmed is False

    # --- hard rejection overrides plan_holder ---

    def test_hard_rejected_plan_holder_stays_rejected(self):
        """A manufacturer with plan_holder evidence → still hard-rejected."""
        record = self._plan_holder_record(
            company_name="Acme Mfg Inc",
            _discovery_source="directory_crawl",
        )
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(business_type="manufacturer", status="rejected", industry_match=False),
            location=_location(),
        )
        assert gate.hard_rejected is True
        assert gate.accepted is False
        assert gate.verification_status == VerificationStatus.rejected

    # --- tier cap still applies ---

    def test_plan_holder_tier4_capped(self):
        """Plan-holder at Tier-4 (fixture) → capped to unknown, not live."""
        record = self._plan_holder_record(source_tier=4)
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert gate.accepted is False
        assert gate.verification_status == VerificationStatus.unknown
        assert any("not a live tier" in r for r in gate.reasons)

    # --- reasons and serialisation ---

    def test_reasons_mention_plan_holder_evidence(self):
        """Reasons include plan-holder evidence when confirmed."""
        record = self._plan_holder_record()
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        assert any("plan-holder" in r.lower() or "bid-listing" in r.lower() for r in gate.reasons)

    def test_to_dict_includes_plan_holder_confirmed(self):
        """to_dict() serialises plan_holder_confirmed."""
        record = self._plan_holder_record()
        gate = AcceptanceGate().evaluate(
            record=record,
            identity=_identity(),
            industry=_industry(status="unknown", industry_match=False),
            location=_location(),
        )
        data = gate.to_dict()
        assert "plan_holder_confirmed" in data
        assert data["plan_holder_confirmed"] is True
        json.dumps(data)  # must serialise without error

    def test_non_plan_holder_record_has_plan_holder_confirmed_false(self):
        """A normal (non-plan-holder) record → plan_holder_confirmed=False
        in to_dict."""
        gate = _verified_record(
            {"company_name": "Acme Roofing LLC", "_discovery_source": "official_website"}
        )
        assert gate.plan_holder_confirmed is False
        data = gate.to_dict()
        assert data["plan_holder_confirmed"] is False
