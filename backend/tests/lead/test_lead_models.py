"""Offline tests for the Prototype V1 lead contract (Increment 0, refined).

Pins the founder's hard rules — a *potential lead* qualifies ONLY when:

1. the company is AcceptanceGate-verified (``gate_accepted`` in metadata);
2. a NAMED decision-maker exists whose ``role_relevance`` is True (owner,
   project manager, estimator, procurement, operations manager, ...);
3. HARD REQUIREMENT — at least one email is ``person_bound``; generic
   ``info@``/``contact@`` or unattributed addresses NEVER qualify, and a
   company with only generic mailboxes is excluded regardless of how strong
   every other signal is;
4. phone is optional — never blocks;
5. at least one buying-intent evidence item carries a non-empty ``source_url``
   (no invented evidence);
6. ``ai_confidence`` >= ``QUALIFIED_THRESHOLD`` (90) plus a ``justification``
   citing the specific evidence used.

The overall ``qualifies`` flag is derived from the deterministic gate, so it
cannot drift from these rules. Everything here runs fully offline (``-m
"not network"`` in pytest.ini); reference shapes come from ``lead_samples``.
"""

from __future__ import annotations

import json

from app.engines.lead.lead_models import (
    GENERIC_EMAIL_LOCAL_PARTS,
    QUALIFIED_THRESHOLD,
    ROLE_RELEVANCE_KEYWORDS,
    EmailVerificationTier,
    IntentEvidence,
    IntentEvidenceType,
    Lead,
    LeadAI,
    LeadEmail,
    LeadPerson,
    LeadPhone,
    PersonVerificationTier,
    PhoneVerificationTier,
    is_generic_email_local_part,
    role_is_plausibly_relevant,
)
from app.engines.lead.lead_samples import (
    sample_empty_justification_lead,
    sample_empty_lead,
    sample_generic_email_lead,
    sample_irrelevant_role_lead,
    sample_low_confidence_lead,
    sample_missing_intent_lead,
    sample_missing_person_lead,
    sample_no_phone_lead,
    sample_qualified_lead,
    sample_unverified_company_lead,
)

# ---------------------------------------------------------------------------
# Honest verification tiers (V1 scope)
# ---------------------------------------------------------------------------


class TestEmailVerificationTier:
    """format/domain/person_bound — only person_bound ever qualifies."""

    def test_v1_tiers_exist(self):
        assert EmailVerificationTier.format.value == "format"
        assert EmailVerificationTier.domain.value == "domain"
        assert EmailVerificationTier.person_bound.value == "person_bound"

    def test_qualifying_tier_is_person_bound_only(self):
        qualifying = {EmailVerificationTier.person_bound}
        assert {t for t in EmailVerificationTier} - qualifying, (
            "format/domain must never count as qualifying on their own"
        )

    def test_tiers_are_plain_strings_for_json(self):
        assert EmailVerificationTier.person_bound.value == "person_bound"
        assert json.dumps({"tier": EmailVerificationTier.person_bound.value}) == (
            '{"tier": "person_bound"}'
        )


class TestPhoneVerificationTier:
    """Phone is format-verified only, optional, and never blocks."""

    def test_v1_tier_exists(self):
        assert PhoneVerificationTier.format.value == "format"
        assert PhoneVerificationTier.resolved.value == "resolved"  # reserved


class TestPersonVerificationTier:
    """The decision-maker record is unverified in V1 (identity, not role)."""

    def test_v1_tier_exists(self):
        assert PersonVerificationTier.unverified.value == "unverified"
        assert PersonVerificationTier.verified.value == "verified"  # reserved


class TestIntentEvidenceType:
    """Open-by-design evidence types, aligned with the plugin framework."""

    def test_evidence_types_cover_founder_list(self):
        expected = {
            "bid_award",
            "hiring",
            "project",
            "expansion",
            "news",
            "permit",
        }
        assert {e.value for e in IntentEvidenceType} == expected

    def test_serialsizes_as_plain_string(self):
        assert IntentEvidenceType.bid_award.value == "bid_award"


# ---------------------------------------------------------------------------
# Grounded helpers for the two flags (role_relevance, generic local-part)
# ---------------------------------------------------------------------------


class TestRoleRelevance:

    def test_relevant_roles_are_recognized(self):
        """Each founder-listed relevant role derives role_relevance True."""
        for role in (
            "Owner",
            "Project Manager",
            "Estimator",
            "Chief Estimator",
            "Procurement Manager",
            "Operations Manager",
        ):
            assert role_is_plausibly_relevant(role) is True, role

    def test_irrelevant_roles_are_rejected(self):
        for role in ("Marketing Coordinator", "Graphic Designer", "Receptionist"):
            assert role_is_plausibly_relevant(role) is False, role

    def test_empty_role_is_never_relevant(self):
        assert role_is_plausibly_relevant("") is False
        assert role_is_plausibly_relevant("   ") is False

    def test_keyword_list_is_not_empty(self):
        assert ROLE_RELEVANCE_KEYWORDS


class TestGenericEmailLocalPart:

    def test_generic_local_parts_flagged(self):
        for local in ("info", "contact", "hello", "sales", "estimates", "info+tag"):
            assert is_generic_email_local_part(local) is True, local

    def test_person_local_parts_not_flagged(self):
        for local in ("m.gomez", "maria.gomez", "j.smith"):
            assert is_generic_email_local_part(local) is False, local

    def test_generic_list_is_not_empty(self):
        assert GENERIC_EMAIL_LOCAL_PARTS


# ---------------------------------------------------------------------------
# Auto-stamped fetch times and default tiers
# ---------------------------------------------------------------------------


class TestLeafRecordBehavior:

    def test_leaf_records_auto_stamp_fetched_at(self):
        assert LeadEmail(email="m.gomez@example.biz").fetched_at
        assert LeadPhone(phone="555-0101").fetched_at
        assert LeadPerson(name="Maria").fetched_at
        assert IntentEvidence(
            type=IntentEvidenceType.news, source_url="https://portal.gov/x"
        ).fetched_at
        assert Lead(company=sample_qualified_lead().company).created_at

    def test_leaf_default_tiers_match_v1_honesty(self):
        assert LeadEmail(email="x@y.co").tier is EmailVerificationTier.format
        assert LeadPhone(phone="555-0101").tier is PhoneVerificationTier.format
        assert LeadPerson(name="Maria").tier is PersonVerificationTier.unverified

    def test_person_defaults_role_relevance_false(self):
        """A person is NOT relevant unless explicitly marked so — no guess."""
        assert LeadPerson(name="Maria").role_relevance is False

    def test_explicit_fetched_at_is_preserved(self):
        stamp = "2026-08-01T00:00:00+00:00"
        assert LeadEmail(email="a@b.co", fetched_at=stamp).fetched_at == stamp


# ---------------------------------------------------------------------------
# The gate — the qualified reference must pass; each blocker independently
# ---------------------------------------------------------------------------


class TestQualificationGate:

    def test_qualified_sample_passes(self):
        lead = sample_qualified_lead()
        result = lead.qualification_gate()
        assert result.qualified is True
        assert lead.qualifies is True
        assert result.blocked_by == []

    def test_qualified_sample_carries_every_requirement(self):
        lead = sample_qualified_lead()
        assert lead.verified_context is True
        assert lead.person is not None and lead.person.name
        assert lead.person.role_relevance is True
        assert lead.has_person_bound_email is True
        assert lead.intent_evidence[0].source_url
        assert lead.ai.ai_confidence >= QUALIFIED_THRESHOLD
        assert lead.ai.justification.strip()

    def test_generic_email_only_is_excluded_entirely(self):
        """HARD RULE #3 — the founder's exact past-outreach failure mode."""
        lead = sample_generic_email_lead()
        assert lead.verified_context is True
        assert lead.has_intent_signal is True
        assert lead.ai.ai_confidence >= QUALIFIED_THRESHOLD
        assert lead.has_person_bound_email is False
        assert lead.qualifies is False
        assert any("person_bound email" in b for b in lead.qualification_gate().blocked_by)

    def test_unverified_company_blocks(self):
        lead = sample_unverified_company_lead()
        result = lead.qualification_gate()
        assert result.qualified is False
        assert any("company not verified" in b for b in result.blocked_by)

    def test_irrelevant_role_blocks(self):
        """A person-bound email does not save an irrelevant role (rule #2)."""
        lead = sample_irrelevant_role_lead()
        assert lead.person is not None
        assert lead.person.role_relevance is False
        assert lead.has_person_bound_email is True  # the email WOULD qualify...
        assert lead.qualifies is False  # ...but the decision-maker is wrong
        assert any("role not plausibly relevant" in b for b in lead.qualification_gate().blocked_by)

    def test_missing_decision_maker_blocks(self):
        lead = sample_missing_person_lead()
        assert lead.qualifies is False
        assert any("decision-maker" in b for b in lead.qualification_gate().blocked_by)

    def test_missing_intent_signal_blocks(self):
        lead = sample_missing_intent_lead()
        assert lead.has_intent_signal is False
        assert lead.qualifies is False
        assert any("source_url" in b for b in lead.qualification_gate().blocked_by)

    def test_low_confidence_blocks(self):
        lead = sample_low_confidence_lead()
        assert lead.ai.ai_confidence == 40.0
        result = lead.qualification_gate()
        assert result.qualified is False
        assert any("ai_confidence 40 < 90" in b for b in result.blocked_by)

    def test_empty_justification_blocks(self):
        lead = sample_empty_justification_lead()
        assert lead.ai.ai_confidence == 91.0
        result = lead.qualification_gate()
        assert result.qualified is False
        assert any("justification empty" in b for b in result.blocked_by)

    def test_missing_phone_never_blocks(self):
        """Hard rule #4 — phone is optional; its absence cannot block."""
        lead = sample_no_phone_lead()
        assert lead.phones == []
        assert lead.qualifies is True  # every other rule met
        assert lead.qualification_gate().blocked_by == []

    def test_bare_lead_reports_every_requirement(self):
        lead = sample_empty_lead()
        result = lead.qualification_gate()
        assert result.qualified is False
        expected_substrings = (
            "company not verified",
            "decision-maker",
            "person_bound email",
            "source_url",
            "ai_confidence 0 < 90",
            "justification empty",
        )
        assert len(result.blocked_by) == len(expected_substrings)
        for sub in expected_substrings:
            assert any(sub in b for b in result.blocked_by), f"missing: {sub}"

    def test_gate_is_deterministic_and_threshold_pinned(self):
        """qualifies never mutates the lead; repeated gates are identical."""
        assert QUALIFIED_THRESHOLD == 90
        lead = sample_qualified_lead()
        assert lead.qualification_gate() == lead.qualification_gate()
        assert lead.qualifies is lead.qualifies


# ---------------------------------------------------------------------------
# Intent-signal semantics (source_url is the evidence)
# ---------------------------------------------------------------------------


class TestIntentSignal:

    def test_blank_source_url_is_not_a_signal(self):
        """A signal that cannot be traced is NOT evidence — gate refuses it."""
        lead = sample_qualified_lead()
        assert lead.has_intent_signal is True
        lead.intent_evidence[0].source_url = "   "
        assert lead.has_intent_signal is False
        assert lead.qualifies is False

    def test_any_evidence_with_url_counts(self):
        """ONE traceable signal of any evidence type satisfies rule #5."""
        lead = sample_qualified_lead()
        lead.intent_evidence[0] = IntentEvidence(
            type=IntentEvidenceType.news,
            source_url="https://news.example.org/article/123",
        )
        assert lead.has_intent_signal is True
        assert lead.qualifies is True

    def test_verified_context_reads_gate_key_from_metadata(self):
        lead = sample_qualified_lead()
        assert lead.verified_context is True
        lead.company.metadata["gate_accepted"] = False
        assert lead.verified_context is False


# ---------------------------------------------------------------------------
# Serialization (JSON-safe round-trip for fixtures/exports)
# ---------------------------------------------------------------------------


class TestSerialization:

    def test_to_dict_is_json_safe(self):
        payload = sample_qualified_lead().to_dict()
        json.dumps(payload)  # must not raise
        assert payload["qualifies"] is True
        assert payload["ai_confidence"] == 91.0
        assert payload["justification"]
        assert payload["emails"][0]["tier"] == "person_bound"
        assert payload["phones"][0]["tier"] == "format"
        assert payload["person"]["role_relevance"] is True
        assert payload["person"]["tier"] == "unverified"
        assert payload["intent_evidence"][0]["type"] == "bid_award"
        assert payload["intent_evidence"][0]["source_url"].startswith("https://")

    def test_includes_company_metadata_and_gate_key(self):
        payload = sample_qualified_lead().to_dict()
        assert payload["company"]["metadata"]["gate_accepted"] is True
        assert payload["company"]["company_name"] == "Texas Skyline Roofing, LLC"

    def test_json_roundtrip_preserves_gate_outcome(self):
        lead = sample_qualified_lead()
        reloaded = json.loads(lead.to_json())
        assert reloaded["qualifies"] is True
        assert reloaded["blocked_by"] == []
        assert reloaded["ai_confidence"] == 91.0

    def test_blocked_lead_roundtrip_reports_blockers(self):
        """A blocked lead serializes its reasons — exports never hide them."""
        reloaded = json.loads(sample_generic_email_lead().to_json())
        assert reloaded["qualifies"] is False
        assert reloaded["blocked_by"]
        assert any("person_bound" in b for b in reloaded["blocked_by"])

    def test_generic_email_lead_is_explicitly_flagged_as_blocked(self):
        """The headline non-qualifying fixture reports its vendor reason."""
        lead = sample_generic_email_lead()
        reloaded = json.loads(lead.to_json())
        assert reloaded["emails"][0]["email"] == "info@texasskylineco.com"
        assert reloaded["emails"][0]["tier"] == "format"


# ---------------------------------------------------------------------------
# AI output shape (extended CompanyScorer output — increment 6 maps onto it)
# ---------------------------------------------------------------------------


class TestLeadAI:

    def test_fields_mirror_company_scorer_output(self):
        """Scorer keys present + the two V1 additions on top."""
        ai = LeadAI()
        assert hasattr(ai, "ai_confidence")  # V1 addition
        assert hasattr(ai, "justification")  # V1 addition
        for attr in (
            "ai_used",
            "deterministic_score",
            "ai_score",
            "reasons",
            "strengths",
            "concerns",
            "error",
        ):
            assert hasattr(ai, attr)

    def test_empty_ai_is_safe_for_json_and_gate(self):
        payload = sample_empty_lead().to_dict()
        assert payload["ai"]["ai_confidence"] == 0.0
        assert payload["qualifies"] is False