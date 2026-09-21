"""Phase 5 safe outreach and Company Intelligence public API."""

from app.research.outreach.engine import build_company_intelligence, build_outreach_trigger
from app.research.outreach.models import OutreachStrength, OutreachTriggerRecord, new_trigger_id

__all__ = [
    "OutreachStrength",
    "OutreachTriggerRecord",
    "build_company_intelligence",
    "build_outreach_trigger",
    "new_trigger_id",
]
