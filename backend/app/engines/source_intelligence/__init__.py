"""Construction Source Intelligence Engine – public source discovery.

Plans which public sources to target for company discovery based on
industry, country, state, and city. Returns a ranked list of sources
without performing any crawling.
"""

from __future__ import annotations

import logging

from app.engines.source_intelligence.source_planner import SourcePlanner
from app.engines.source_intelligence.source_models import (
    SourceRecord,
    SourcePlannerRequest,
    SourcePlannerResult,
)

logger = logging.getLogger(__name__)
