"""Prototype V1 — the potential-lead record contract.

BUSINESS DEFINITION (founder-agreed, threshold-only for V1):

The Best Estimator LLC sells construction estimation services. A *potential
lead* is a company that is provably in a buying window AND has a person we
can actually reach who owns estimation/bidding decisions — NOT a company
with a generic mailbox. A record qualifies ONLY when EVERY hard rule holds:

1. Verified company (``AcceptanceGate`` — ``company.metadata["gate_accepted"]``).
2. A named decision-maker whose ``role_relevance`` is True — their role must
   plausibly touch estimation/bidding/project decisions (owner, project
   manager, estimator, procurement, operations manager, ...), not just any
   name found on the site.
3. HARD REQUIREMENT — a ``person_bound`` email: an address specifically
   bound to that person (e.g. ``f.last@company.com``). Generic mailboxes
   (``info@``, ``contact@``, ...) do NOT qualify, and a company with only
   generic addresses is excluded regardless of how strong every other
   signal is.
4. Phone is OPTIONAL — a nice-to-have that never blocks qualification.
5. At least one buying-intent evidence item with a traceable ``source_url``
   (won a bid, active hiring, expansion, relevant news) — never invented
   evidence.
6. ``ai_confidence`` >= ``QUALIFIED_THRESHOLD`` (90) ONLY when real evidence
   backs it, plus a ``justification`` that cites the specific evidence used.

Zero-budget constraint: every signal comes from free/keyless tools — this
module defines only the container; evidence provenance is additive.

Accuracy-first rules carried here (CLAUDE.md §1, §10):
    - "no evidence = unknown, not true": an empty lead is never qualified,
      and the gate reports EVERY missing requirement instead of silently
      passing a partial lead;
    - the gate is DETERMINISTIC (mirrors the Phase 2 AcceptanceGate) — AI is
      one requirement among several, never an override;
    - ``qualifies`` is DERIVED from the gate, so it can never drift from the
      hard rules above.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.engines.discovery.company.company_models import CompanyDiscoveryResult

#: AI confidence (0-100) a lead must reach to clear the V1 gate. A lead never
#: clears on score alone — real evidence is required (see ``Lead.qualification_gate``).
QUALIFIED_THRESHOLD = 90

#: Roles plausibly relevant to estimation/bidding/project decisions (V1).
#: Long, unambiguous phrases, matched as substrings of ``LeadPerson.role``.
#: Founder list: owner, project manager, operations manager, presidents/VPs —
#: the people who DECIDE to buy estimation services. Estimators/estimating,
#: procurement and purchasing are deliberately EXCLUDED: those are the people
#: who DO estimation in-house (the competitor, not the buyer). Anything outside
#: these is not enough to qualify. Short acronyms (CEO/CFO/COO/VP/PM/OPS) are
#: matched ONLY as whole tokens via ``ROLE_RELEVANCE_ACRONYMS`` so "coo" inside
#: "Coordinator" can never hit.
ROLE_RELEVANCE_KEYWORDS = (
    "chief executive",
    "chief operating",
    "chief financial",
    "managing partner",
    "managing member",
    "vice president",
    "operations manager",
    "ops manager",
    "operations",
    "general manager",
    "project manager",
    "project lead",
    "director of",
    "president",
    "principal",
    "partner",
    "leadership",
    "founder",
    "owner",
)

#: Short abbreviations that count only when they appear as a WHOLE word —
#: never as a substring (a "coo" inside "coordinator" is not a C.O.O.).
ROLE_RELEVANCE_ACRONYMS = frozenset({"ceo", "cfo", "coo", "vp", "pm", "ops"})


def _role_tokens(text: str) -> set[str]:
    """Split a lowercase role into alphanumeric-only whole-word tokens."""
    scrubbed = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return {t for t in scrubbed.split() if t}


def role_is_plausibly_relevant(role: str) -> bool:
    """True when ``role`` plausibly touches estimation/bidding/project decisions.

    Empty roles are never relevant. Long keywords match as substrings; short
    acronyms (CEO/CFO/COO/VP/PM/OPS) must be whole tokens — a role like
    "Marketing Coordinator" is correctly rejected. ``LeadPerson.role_relevance``
    is derived with this helper.
    """
    text = role.strip().lower()
    if not text:
        return False
    if any(key in text for key in ROLE_RELEVANCE_KEYWORDS):
        return True
    return bool(_role_tokens(text) & ROLE_RELEVANCE_ACRONYMS)


#: Email local-parts that are generic mailbox addresses, never bound to a
#: specific person (founder hard rule #3 — these do NOT qualify a lead).
GENERIC_EMAIL_LOCAL_PARTS = {
    "info",
    "contact",
    "hello",
    "general",
    "support",
    "sales",
    "admin",
    "office",
    "mail",
    "inbox",
    "accounts",
    "billing",
    "careers",
    "jobs",
    "hr",
    "webmaster",
    "service",
    "customerservice",
    "frontdesk",
    "reception",
    "estimates",
    "quotes",
    "proposals",
    "team",
}


def is_generic_email_local_part(local: str) -> bool:
    """True when the email local-part is a generic mailbox (info, contact, ...).

    Subaddressing (``m.gomez+tag@``) still maps to the base local-part. A
    generic local-part can NEVER be ``person_bound`` regardless of label.
    """
    base = local.strip().lower().split("+")[0]
    return base in GENERIC_EMAIL_LOCAL_PARTS


def _now_iso() -> str:
    """Current UTC timestamp as an ISO string with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Honest verification tiers (V1 scope)
# ---------------------------------------------------------------------------


class EmailVerificationTier(str, Enum):
    """Verification tier for a discovered email address.

    ``person_bound`` is the ONLY qualifying tier in V1 (hard rule #3): the
    address is specifically bound to the decision-maker. ``format`` and
    ``domain`` describe generic/discovered addresses that never qualify a
    lead on their own.
    """

    format = "format"  # passes syntax + dedupe only (generic or unattributed)
    domain = "domain"  # additionally: the domain's MX record resolves
    person_bound = "person_bound"  # specifically bound to the decision-maker


class PhoneVerificationTier(str, Enum):
    """Verification tier for a discovered phone number (V1: format only).

    Phone is optional and never blocks qualification (hard rule #4).
    """

    format = "format"  # passes normalization / regex extraction
    resolved = "resolved"  # reserved: confirmed to belong to the decision-maker


class PersonVerificationTier(str, Enum):
    """Identity-verification tier for the decision-maker record (V1).

    ``unverified`` is the honest V1 default: the identity is site-derived
    best-effort. ``role_relevance`` (which gates qualification) is separate
    from identity verification — see ``LeadPerson``.
    """

    unverified = "unverified"  # site-derived best-effort (PeopleParser)
    verified = "verified"  # reserved: person identity confirmed


class IntentEvidenceType(str, Enum):
    """Type of buying-intent signal for a company.

    Values map onto the existing plugin capability framework (hard
    constraint: reuse, don't build new discovery architecture):
    ``bid_award``/``hiring``/``project`` -> BID_DISCOVERY / NEWS-style /
    PROJECT_DISCOVERY, ``news`` -> NEWS_DISCOVERY, ``permit`` ->
    PERMIT_DISCOVERY. Open by design (a ``str`` enum): a new evidence type
    serializes and filters without a schema change.
    """

    bid_award = "bid_award"  # won a bid / contract  (BID_DISCOVERY)
    hiring = "hiring"  # active hiring for relevant roles
    project = "project"  # current/recent project on the company site (PROJECT_DISCOVERY)
    expansion = "expansion"  # new office / location / growth announcement
    news = "news"  # local news / press mentioning growth or a new project (NEWS_DISCOVERY)
    permit = "permit"  # recent construction permit filed (PERMIT_DISCOVERY)


# ---------------------------------------------------------------------------
# Leaf records
# ---------------------------------------------------------------------------


@dataclass
class LeadEmail:
    """One email address attached to the lead, with an honest tier.

    Only ``tier == person_bound`` counts toward qualification (hard rule #3).
    """

    email: str
    tier: EmailVerificationTier = EmailVerificationTier.format
    source_url: str = ""
    fetched_at: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "tier": self.tier.value,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
        }


@dataclass
class LeadPhone:
    """One phone number attached to the lead (optional, format tier only)."""

    phone: str
    tier: PhoneVerificationTier = PhoneVerificationTier.format
    source_url: str = ""
    fetched_at: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "phone": self.phone,
            "tier": self.tier.value,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
        }


@dataclass
class LeadPerson:
    """A specific decision-maker for the target company.

    ``role_relevance`` is the V1 gate criterion: the role MUST plausibly
    touch estimation/bidding/project decisions (hard rule #2). Derive it with
    ``role_is_plausibly_relevant``. ``tier`` records identity verification
    (reserved future) and does NOT gate qualification in V1.
    """

    name: str
    role: str = ""
    role_relevance: bool = False  # role plausibly touches estimation/bidding
    tier: PersonVerificationTier = PersonVerificationTier.unverified
    source_url: str = ""
    fetched_at: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "role_relevance": self.role_relevance,
            "tier": self.tier.value,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
        }


@dataclass
class IntentEvidence:
    """One piece of buying-intent evidence for the company.

    ``source_url`` is the single most important field: a signal that cannot
    be traced back to a URL is NOT evidence (hard rule #5 — no invented
    evidence), and the gate refuses to qualify a lead on it.
    """

    type: IntentEvidenceType
    source_url: str  # required — the signal must be traceable
    snippet: str = ""  # short quote/fragment backing the signal
    date: str = ""  # when the signal occurred (ISO date or free text)
    source: str = ""  # origin that produced it (directory / google_news / usaspending / plugin)
    fetched_at: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "source_url": self.source_url,
            "snippet": self.snippet,
            "date": self.date,
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


@dataclass
class LeadAI:
    """AI qualification output for a lead (extended CompanyScorer output).

    Field names deliberately mirror ``CompanyScorer.qualify`` output so later
    increments (extending that ONE scorer, per the no-second-AI-system rule)
    map verbatim onto this object; ``ai_confidence`` and ``justification`` are
    the V1 additions. ``qualifies`` on the Lead (not here) is the gate verdict.
    """

    ai_confidence: float = 0.0  # 0-100 — must reach QUALIFIED_THRESHOLD to qualify
    justification: str = ""  # written reasoning citing SPECIFIC evidence
    qualified: bool = False  # the scorer's own verdict (informational)
    ai_used: bool = False
    deterministic_score: float = 0.0
    ai_score: float | None = None
    reasons: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ai_confidence": self.ai_confidence,
            "justification": self.justification,
            "qualified": self.qualified,
            "ai_used": self.ai_used,
            "deterministic_score": self.deterministic_score,
            "ai_score": self.ai_score,
            "reasons": list(self.reasons),
            "strengths": list(self.strengths),
            "concerns": list(self.concerns),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# The lead record + deterministic gate
# ---------------------------------------------------------------------------


@dataclass
class LeadGateResult:
    """Deterministic outcome of the V1 threshold gate."""

    qualified: bool
    blocked_by: list[str] = field(default_factory=list)


@dataclass
class Lead:
    """One potential-lead record: company + person + contacts + intent + AI.

    ``company`` reuses :class:`CompanyDiscoveryResult` (rule #14); its
    ``metadata["gate_accepted"]`` marks AcceptanceGate acceptance, and
    ``metadata["verification"]`` carries the Phase-2 verification namespace.
    """

    company: CompanyDiscoveryResult
    person: LeadPerson | None = None
    emails: list[LeadEmail] = field(default_factory=list)
    phones: list[LeadPhone] = field(default_factory=list)
    intent_evidence: list[IntentEvidence] = field(default_factory=list)
    ai: LeadAI = field(default_factory=LeadAI)
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()

    @property
    def verified_context(self) -> bool:
        """True when the company passed the deterministic AcceptanceGate."""
        return bool(self.company.metadata.get("gate_accepted", False))

    @property
    def has_person_bound_email(self) -> bool:
        """True when at least one email is ``person_bound`` — the hard rule."""
        return any(e.tier is EmailVerificationTier.person_bound for e in self.emails)

    @property
    def has_intent_signal(self) -> bool:
        """True when at least one evidence carries a non-empty ``source_url``."""
        return any(e.source_url.strip() for e in self.intent_evidence)

    def qualification_gate(self) -> LeadGateResult:
        """Evaluate the V1 gate, deterministically, against EVERY hard rule.

        A lead clears ONLY when every requirement is met; the gate returns
        every unsatisfied requirement (``blocked_by``) so a partial lead
        reports what is missing instead of silently passing. AI is one
        requirement, never an override of the evidence checks. Phones are
        intentionally absent — a missing phone never blocks (hard rule #4).
        """
        blocked: list[str] = []
        # Rule 1 — verified company context
        if not self.verified_context:
            blocked.append("company not verified (AcceptanceGate gate_accepted is False)")
        # Rule 2 — named decision-maker with a relevant role
        if self.person is None or not (self.person.name or "").strip():
            blocked.append("no named decision-maker (person.name empty)")
        elif not self.person.role_relevance:
            blocked.append(
                "decision-maker role not plausibly relevant to "
                "estimation/bidding (role_relevance False)"
            )
        # Rule 3 — HARD: a person-bound email is required; generic never qualifies
        if not self.has_person_bound_email:
            blocked.append(
                "no person_bound email — info@/contact@ or unattributed "
                "addresses never qualify (email tier must be person_bound)"
            )
        # Rule 5 — at least one intent signal with a traceable source URL
        if not self.has_intent_signal:
            blocked.append("no buying-intent evidence with a source_url")
        # Rule 6 — confidence and evidence-cited justification
        if self.ai.ai_confidence < QUALIFIED_THRESHOLD:
            blocked.append(
                f"ai_confidence {self.ai.ai_confidence:.0f} < {QUALIFIED_THRESHOLD}"
            )
        if not (self.ai.justification or "").strip():
            blocked.append("justification empty (must cite the specific evidence used)")
        return LeadGateResult(qualified=not blocked, blocked_by=blocked)

    @property
    def qualifies(self) -> bool:
        """Overall V1 verdict — True ONLY when every hard rule is met.

        Derived from the gate every call, so it can never drift from the rules.
        """
        return self.qualification_gate().qualified

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (enums -> their value strings).

        Top-level ``ai_confidence`` / ``justification`` / ``qualifies`` are
        the primary schema fields; ``ai`` additionally carries the full
        CompanyScorer-attribution detail block for later increments.
        """
        gate = self.qualification_gate()
        return {
            "company": asdict(self.company),
            "person": self.person.to_dict() if self.person else None,
            "emails": [e.to_dict() for e in self.emails],
            "phones": [p.to_dict() for p in self.phones],
            "intent_evidence": [e.to_dict() for e in self.intent_evidence],
            "ai_confidence": self.ai.ai_confidence,
            "justification": self.ai.justification,
            "qualifies": gate.qualified,
            "blocked_by": gate.blocked_by,
            "ai": self.ai.to_dict(),
            "created_at": self.created_at,
        }

    def to_json(self, *, indent: int = 2) -> str:
        """Serialize to an indented JSON string (for fixtures/exports)."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)