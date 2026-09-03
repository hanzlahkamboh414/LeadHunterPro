"""Person Attribution Research — V1.

Additive side-band that researches an otherwise unattributed company email
(e.g. ``abc@abcconstruction.com`` with no known person) and determines whether
it can be reliably attributed to a specific person. See
``docs/person_research_v1_spec.md`` for the locked spec.
"""

from app.person_research.models import (
    AttributionVerdict,
    PersonCandidate,
    ResearchEvidence,
    ResearchResult,
    ResearchStatus,
)
from app.person_research.orchestrator import ResearchApplier, ResearchOrchestrator
from app.person_research.scoring import LocalPartMatcher, ResearchScorer
from app.person_research.search_adapter import RegistryIndexedSearch
from app.person_research.service import ResearchService
from app.person_research.sources import DomainFacts, IndexedWebSource, WebsiteSource
from app.person_research.store import ResearchStore, email_hash, normalize_email

__all__ = [
    "AttributionVerdict",
    "PersonCandidate",
    "ResearchEvidence",
    "ResearchResult",
    "ResearchStatus",
    "ResearchApplier",
    "ResearchOrchestrator",
    "LocalPartMatcher",
    "ResearchScorer",
    "RegistryIndexedSearch",
    "ResearchService",
    "DomainFacts",
    "IndexedWebSource",
    "WebsiteSource",
    "ResearchStore",
    "email_hash",
    "normalize_email",
]
