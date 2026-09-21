"""Phase 3 deterministic signal engine public API."""

from app.research.signals.engine import compute_company_signals, recency_bucket
from app.research.signals.models import (
    RecencyBucket,
    SignalComputationResult,
    SignalRecord,
    SignalStrength,
    SignalType,
    new_signal_id,
)

__all__ = [
    "RecencyBucket",
    "SignalComputationResult",
    "SignalRecord",
    "SignalStrength",
    "SignalType",
    "compute_company_signals",
    "new_signal_id",
    "recency_bucket",
]
