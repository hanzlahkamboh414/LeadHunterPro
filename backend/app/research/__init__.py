"""Company Signal Intelligence Engine — research evidence package.

The engine that replaces *"AI → search → guess pain → write email"* with
*"evidence → event → signal → pain → gate → outreach"*. Each layer is a
module here, and each is either fully deterministic or a single bounded AI
call whose output a deterministic layer then validates.

Founder-frozen architecture (2026-09-18)::

    L0  Identity Resolver        store.resolve_company
    L1  Research Planner         (Phase 1)
    L2  Existing Source Registry app.source_scout.store — NOT duplicated here
    L3  Existing Query/Discovery app.search_providers / search_cache
    L4  Existing Intent Plugins  app.discovery.intent — NOT duplicated here
    L5  Canonical Evidence Store store.ResearchEvidenceStore
    L6  AI Call #1  Evidence→Events        (Phase 2)
    L7  Deterministic Event Normalization  taxonomy + (Phase 2 engine)
    L8  Deterministic Signal Engine        (Phase 3)
    L9  AI Call #2  Events→Candidate Pain  (Phase 4)
    L10 Deterministic PAIN_GATE            (Phase 4, modeled on
                                            Lead.qualification_gate)
    then Trigger + Outreach

Two boundaries this package exists to hold:

**AI proposes, the deterministic engine decides.** AI Call #2 may suggest a
pain; it may not conclude one, and it may not go back to the raw web for
support — its input is verified events plus deterministic metadata. The
final verdict is the chain *contradiction check → recency → evidence count
and independence → PAIN_GATE*. Without that split, ``check.txt``'s original
defect simply returns under a new name: *AI says pain, so the system
believes pain.*

**The four honest states never collapse.** ``NOT_FOUND``,
``NOT_ACCESSIBLE``, ``NOT_VERIFIED`` and ``VERIFIED``
(:class:`~app.research.taxonomy.ResearchState`) are stored and displayed as
four facts. "We did not find it" is not "it does not exist".

Phase 0 established the foundation:
:mod:`~app.research.taxonomy` (event/evidence vocabulary and the
procurement-chain guard), :mod:`~app.research.models` (the canonical
evidence record), :mod:`~app.research.adapters` (the three legacy evidence
models projected onto it) and :mod:`~app.research.store` (the separate
``research_evidence.db``). Phase 1 wires canonical evidence intake into the
production research path. Phase 2 adds :mod:`~app.research.events`: one
bounded evidence-to-events AI call followed by deterministic identity,
taxonomy, provenance, procurement-stage, permit and date validation. Phase 3
adds :mod:`~app.research.signals`: deterministic recency, correlation,
weighting, project deduplication and contradiction handling.
Phase 4 adds :mod:`~app.research.pain`: one bounded correlation call followed
by the deterministic PAIN_GATE.
"""

from app.research.intake import (
    CompanyIntakeResult,
    collect_company_evidence,
)
from app.research.events import (
    EventExtractionError,
    EventExtractionResult,
    EventRecord,
    extract_company_events,
    new_event_id,
)
from app.research.models import (
    MAX_EXCERPT_CHARS,
    CanonicalEvidence,
    CompanyIdentity,
    content_hash,
    new_evidence_id,
    project_key,
)
from app.research.pain import (
    CandidatePainHypothesis,
    PainGateResult,
    PainHypothesisRecord,
    PainInferenceError,
    PainInferenceResult,
    PainType,
    PainVerdict,
    infer_company_pain,
    new_hypothesis_id,
    pain_basis_hash,
    pain_gate,
)
from app.research.store import (
    ResearchEvidenceStore,
    default_db_path,
    normalize_company_key,
)
from app.research.signals import (
    RecencyBucket,
    SignalComputationResult,
    SignalRecord,
    SignalStrength,
    SignalType,
    compute_company_signals,
    new_signal_id,
    recency_bucket,
)
from app.research.taxonomy import (
    ACTIVITY_ONLY_EVENTS,
    BID_AWARD_CHAIN,
    CONTRACT_AWARD_EVENTS,
    UNRESOLVED_EVENTS,
    CompanyMatch,
    EventType,
    EvidenceType,
    ResearchState,
    chain_rank,
    is_contract_award,
    is_unresolved_event,
    may_promote,
    normalize_event_type,
    promotion_violation,
)

__all__ = [
    "ACTIVITY_ONLY_EVENTS",
    "BID_AWARD_CHAIN",
    "CONTRACT_AWARD_EVENTS",
    "MAX_EXCERPT_CHARS",
    "UNRESOLVED_EVENTS",
    "CanonicalEvidence",
    "CandidatePainHypothesis",
    "CompanyIdentity",
    "CompanyIntakeResult",
    "CompanyMatch",
    "EventType",
    "EvidenceType",
    "EventExtractionError",
    "EventExtractionResult",
    "EventRecord",
    "PainGateResult",
    "PainHypothesisRecord",
    "PainInferenceError",
    "PainInferenceResult",
    "PainType",
    "PainVerdict",
    "ResearchEvidenceStore",
    "ResearchState",
    "RecencyBucket",
    "SignalComputationResult",
    "SignalRecord",
    "SignalStrength",
    "SignalType",
    "chain_rank",
    "collect_company_evidence",
    "compute_company_signals",
    "content_hash",
    "default_db_path",
    "extract_company_events",
    "infer_company_pain",
    "is_contract_award",
    "is_unresolved_event",
    "may_promote",
    "new_evidence_id",
    "new_event_id",
    "new_hypothesis_id",
    "new_signal_id",
    "normalize_company_key",
    "normalize_event_type",
    "pain_basis_hash",
    "pain_gate",
    "project_key",
    "promotion_violation",
    "recency_bucket",
]
