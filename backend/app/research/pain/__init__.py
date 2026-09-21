"""Phase 4 pain-correlation and deterministic gate public API."""

from app.research.pain.engine import (
    PainInferenceError,
    infer_company_pain,
    pain_basis_hash,
    pain_gate,
)
from app.research.pain.models import (
    CandidatePainHypothesis,
    PainGateResult,
    PainHypothesisRecord,
    PainInferenceResult,
    PainType,
    PainVerdict,
    new_hypothesis_id,
)

__all__ = [
    "CandidatePainHypothesis",
    "PainGateResult",
    "PainHypothesisRecord",
    "PainInferenceError",
    "PainInferenceResult",
    "PainType",
    "PainVerdict",
    "infer_company_pain",
    "new_hypothesis_id",
    "pain_basis_hash",
    "pain_gate",
]
