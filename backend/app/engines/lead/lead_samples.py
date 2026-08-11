"""Canonical fixture samples for the V1 lead contract.

These are STRUCTURE samples — deterministic objects that pin the ``Lead``
schema and its hard rules in offline tests. They are NOT discovery output:
names, domains and phone numbers are invented silhouettes (555-series
numbers, unregistered domains) and evidence URLs point at genuine public
portals only to prove shape. They must never enter a production result set
(CLAUDE.md §1, §10) — the real discovery/increment pipeline produces real
leads.

Two headline fixtures, per the founder's brief:
    - ``sample_qualified_lead``        — every hard rule met -> qualifies True;
    - ``sample_generic_email_lead``    — ONLY ``info@`` present, every other
      signal strong -> still blocked (hard rule #3, the whole point).
"""

from __future__ import annotations

from app.engines.discovery.company.company_models import CompanyDiscoveryResult
from app.engines.lead.lead_models import (
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
    role_is_plausibly_relevant,
)

# ---------------------------------------------------------------------------
# Building blocks (shared by every sample)
# ---------------------------------------------------------------------------


def _verified_company() -> CompanyDiscoveryResult:
    """A company record that cleared the AcceptanceGate (carried in metadata)."""
    return CompanyDiscoveryResult(
        company_name="Texas Skyline Roofing, LLC",
        website="https://www.texasskylineco.com",
        city="Dallas",
        state="TX",
        country="USA",
        source="directory",
        confidence=0.9,
        source_url="https://www.texasskylineco.com/about",
        discovery_reason="Matched roofing contractor in Dallas, TX",
        metadata={
            "gate_accepted": True,
            "verification": {
                "accepted": True,
                "verification_status": "verified",
                "business_type": "contractor",
                "city": "Dallas",
                "state": "TX",
            },
        },
    )


def _unverified_company() -> CompanyDiscoveryResult:
    """A company record never evaluated by the AcceptanceGate (no gate key)."""
    return CompanyDiscoveryResult(
        company_name="Texas Skyline Roofing, LLC",
        website="https://www.texasskylineco.com",
        city="Dallas",
        state="TX",
        source="directory",
        metadata={},  # no "gate_accepted" — never cleared the gate
    )


def _decision_maker() -> LeadPerson:
    """Owner — an estimation/bidding-relevant role (role_relevance derived)."""
    return LeadPerson(
        name="Maria Gomez",
        role="Owner",
        role_relevance=role_is_plausibly_relevant("Owner"),
        tier=PersonVerificationTier.unverified,
        source_url="https://www.texasskylineco.com/team",
    )


def _irrelevant_role_person() -> LeadPerson:
    """A real name on the site, but the role does NOT touch bidding/estimating."""
    return LeadPerson(
        name="Dana Smith",
        role="Marketing Coordinator",
        role_relevance=role_is_plausibly_relevant("Marketing Coordinator"),
        tier=PersonVerificationTier.unverified,
        source_url="https://www.texasskylineco.com/about",
    )


def _person_bound_email() -> LeadEmail:
    """HARD RULE #3 — the email is specifically bound to the decision-maker."""
    return LeadEmail(
        email="m.gomez@texasskylineco.com",
        tier=EmailVerificationTier.person_bound,
        source_url="https://www.texasskylineco.com/team",
    )


def _generic_email() -> LeadEmail:
    """A generic mailbox — NEVER a qualifying email, whatever else is strong."""
    return LeadEmail(
        email="info@texasskylineco.com",
        tier=EmailVerificationTier.format,
        source_url="https://www.texasskylineco.com/contact",
    )


def _contact_phone() -> LeadPhone:
    """Format-tier phone only — optional, never blocks (hard rule #4)."""
    return LeadPhone(
        phone="+1-214-555-0134",
        tier=PhoneVerificationTier.format,
        source_url="https://www.texasskylineco.com/contact",
    )


def _bid_award_evidence() -> IntentEvidence:
    """One buying-intent signal with a traceable source URL (hard rule #5)."""
    return IntentEvidence(
        type=IntentEvidenceType.bid_award,
        source_url="https://www.txsmartbuy.com/esbd",
        snippet=(
            "Recent award listing on Texas statewide procurement "
            "for commercial roofing services"
        ),
        date="2026-07-15",
        source="directory",
    )


def _qualified_ai() -> LeadAI:
    """AI output for a qualifying lead: >=90 + justification citing evidence."""
    return LeadAI(
        ai_confidence=91.0,
        justification=(
            "Company cleared the AcceptanceGate (verified contractor in Dallas, TX); "
            "one buying-intent signal present (bid_award, "
            "https://www.txsmartbuy.com/esbd, 2026-07-15); decision-maker Maria "
            "Gomez (Owner) identified on the company site with a person-bound "
            "email (m.gomez@texasskylineco.com)."
        ),
        qualified=True,
        ai_used=True,
        deterministic_score=62.0,
        ai_score=91.0,
        reasons=[
            "verified context (AcceptanceGate)",
            "bid_award evidence with traceable source_url",
            "Owner role is estimation/bidding-relevant",
            "person_bound email present",
        ],
        strengths=["single recent award signal with traceable URL"],
        concerns=["email domain not yet MX-checked", "decision-maker identity unverified"],
    )


#: Sentinel so ``person=None`` (an intentionally absent person) is distinct
#: from "not provided, build the default decision-maker" — never shared mutable
#: defaults across fixtures.
_UNSET = object()


def _compose(
    *,
    company: CompanyDiscoveryResult,
    person: LeadPerson | None | object = _UNSET,
    emails: tuple[LeadEmail, ...] | None = None,
    intent: bool = True,
    ai: LeadAI | None = None,
    with_phone: bool = True,
) -> Lead:
    """Compose a fresh Lead from the shared building blocks (never reused)."""
    if person is _UNSET:
        person = _decision_maker()
    if emails is None:
        emails = (_person_bound_email(),)
    return Lead(
        company=company,
        person=person,  # type: ignore[arg-type]
        emails=list(emails),
        phones=[_contact_phone()] if with_phone else [],
        intent_evidence=[_bid_award_evidence()] if intent else [],
        ai=ai or _qualified_ai(),
    )


# ---------------------------------------------------------------------------
# Headline fixtures: a qualifying example and a non-qualifying example
# ---------------------------------------------------------------------------


def sample_qualified_lead() -> Lead:
    """EVERY hard rule met -> qualifies True. The reference qualifying shape."""
    return _compose(company=_verified_company())


def sample_generic_email_lead() -> Lead:
    """ONLY a generic info@ mailbox despite every other signal being strong.

    The founder's exact failure mode from past outreach — this must be
    blocked regardless of company verification, intent evidence, or score.
    """
    return _compose(company=_verified_company(), emails=(_generic_email(),))


# ---------------------------------------------------------------------------
# Additional non-qualifying fixtures (one blocker each, for the tests)
# ---------------------------------------------------------------------------


def sample_unverified_company_lead() -> Lead:
    """Not AcceptanceGate-verified — blocked even with a person-bound email."""
    return _compose(company=_unverified_company())


def sample_irrelevant_role_lead() -> Lead:
    """Person-bound email exists but the person's role is not bidding/estimating."""
    return _compose(company=_verified_company(), person=_irrelevant_role_person())


def sample_missing_person_lead() -> Lead:
    """No decision-maker person at all."""
    return _compose(company=_verified_company(), person=None)


def sample_missing_intent_lead() -> Lead:
    """No buying-intent evidence — fails the core 'real evidence' rule."""
    return _compose(company=_verified_company(), intent=False)


def sample_low_confidence_lead() -> Lead:
    """Everything present but ai_confidence 40 — score alone can never qualify."""
    ai = _qualified_ai()
    ai.ai_confidence = 40.0
    ai.qualified = False
    return _compose(company=_verified_company(), ai=ai)


def sample_empty_justification_lead() -> Lead:
    """Every gate requirement met except the evidence-cited justification."""
    ai = _qualified_ai()
    ai.justification = ""
    return _compose(company=_verified_company(), ai=ai)


def sample_no_phone_lead() -> Lead:
    """Phone absent — must still qualify (hard rule #4: phone never blocks)."""
    return _compose(company=_verified_company(), with_phone=False)


def sample_empty_lead() -> Lead:
    """A bare record: reports every gap, qualifies False."""
    return Lead(company=_unverified_company())