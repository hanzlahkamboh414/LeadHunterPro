"""Person Attribution Research — production service entry point.

Ties the SQLite store, the orchestrator, and the applier into one object a
caller can use to:

- **research(email, domain)** — run Stages 0-2 and persist the job.
- **rescore(email)** — re-derive scores + verdict from stored evidence with
  NO external calls (spec §10, the ``research_version`` counterpart).
- **apply_to_lead(lead, email)** — write an ``attributed`` result into a lead
  record via :class:`ResearchApplier` (integration boundary).

Live indexed-web search is wired through :class:`RegistryIndexedSearch`, which
reuses the shared search-provider registry and stays swappable (CLAUDE.md §4).
"""

from __future__ import annotations

from typing import Any

from app.person_research.models import ResearchResult, ResearchStatus
from app.person_research.orchestrator import ResearchApplier, ResearchOrchestrator
from app.person_research.search_adapter import RegistryIndexedSearch
from app.person_research.sources import IndexedWebSource
from app.person_research.store import ResearchStore, email_hash


class ResearchService:
    """One object owning the store + orchestrator + applier for callers."""

    def __init__(
        self,
        store: ResearchStore,
        *,
        orchestrator: ResearchOrchestrator | None = None,
        applier: ResearchApplier | None = None,
    ) -> None:
        self.store = store
        self.orchestrator = orchestrator or ResearchOrchestrator(
            indexed_source=IndexedWebSource(search=RegistryIndexedSearch())
        )
        self.applier = applier or ResearchApplier()

    def research(
        self,
        email: str,
        domain: str,
        *,
        research_version: int = 1,
        force: bool = False,
    ) -> ResearchResult:
        """Run the research waterfall for one email and persist the job."""
        eh = self.store.create_job(email, domain, research_version=research_version)
        if force:
            self.store.requeue(eh, force_research=True)
        self.store.mark_researching(eh)
        result = self.orchestrator.research(email, domain)
        self.store.complete_job(result, ResearchStatus.completed)
        return result

    def rescore(self, email: str) -> ResearchResult | None:
        """Re-score a stored job from its evidence. No external calls."""
        eh = email_hash(email)
        job = self.store.get_job(eh)
        if job is None:
            return None
        result = ResearchResult.from_job(job)
        candidates, verdict = self.orchestrator.rescore(result.candidates)
        result.candidates = candidates
        result.verdict = verdict
        self.store.complete_job(result, ResearchStatus.completed)
        return result

    def apply_to_lead(self, lead: dict[str, Any], email: str) -> dict[str, Any]:
        """Apply an attributed stored result to a lead record (if any)."""
        job = self.store.get_job(email_hash(email))
        if job is None:
            return lead
        result = ResearchResult.from_job(job)
        return self.applier.apply(lead, result)
