"""Person Attribution Research — orchestrator (Stages 0-2) + applier.

The orchestrator runs the research waterfall for ONE email (spec §4):

- **Stage 0 — triage**: skip emails with no person identity (free-mail
  domains, generic local parts like ``info@``). Precision > recall.
- **Stage 1 — company website**: scoped crawl -> ``DomainFacts`` (cached).
- **Stage 2 — indexed web**: corroborate candidate names via a search API.

It returns a :class:`ResearchResult`; it does NOT write to the store (the CLI
runner owns persistence). :class:`ResearchApplier` is the integration boundary:
it only ever modifies a lead record when the verdict is ``attributed``.
"""

from __future__ import annotations

from typing import Any

from app.email.email_cleaner import is_free_mail_domain
from app.engines.lead.lead_models import is_generic_email_local_part
from app.person_research.models import (
    AttributionVerdict,
    PersonCandidate,
    ResearchEvidence,
    ResearchResult,
)
from app.person_research.scoring import ResearchScorer
from app.person_research.sources import (
    DomainFacts,
    IndexedWebSource,
    WebsiteSource,
)
from app.person_research.store import email_hash, normalize_email


class ResearchOrchestrator:
    """Runs Stages 0-2 and evaluates the verdict for one email."""

    def __init__(
        self,
        *,
        website_source: WebsiteSource | None = None,
        indexed_source: IndexedWebSource | None = None,
        scorer: ResearchScorer | None = None,
    ) -> None:
        self._website = website_source or WebsiteSource()
        self._indexed = indexed_source or IndexedWebSource()
        self._scorer = scorer or ResearchScorer()

    def research(self, email: str, domain: str, research_version: int = 1) -> ResearchResult:
        target = normalize_email(email)
        eh = email_hash(target)
        base = ResearchResult(email=target, email_hash=eh, domain=domain, attempts=1)

        # -- Stage 0: triage ------------------------------------------------
        triage_reason = _triage_reason(target)
        if triage_reason:
            base.source_errors["triage"] = triage_reason
            base.sources_checked.append("triage")
            return base  # verdict stays unattributed

        # -- Stage 1: company website ----------------------------------------
        facts = self._website.discover(domain, target)
        base.sources_checked.extend(facts.pages)

        # -- Stage 2: indexed web corroboration ------------------------------
        corroborations = _corroborate(self._indexed, facts, target)

        candidates, verdict = self._scorer.evaluate(
            target, domain, facts, corroborations
        )
        base.candidates = candidates
        base.verdict = verdict
        return base

    def rescore(
        self, candidates: list[PersonCandidate]
    ) -> tuple[list[PersonCandidate], AttributionVerdict]:
        """Re-score stored candidates without external calls (spec §10)."""
        return self._scorer.rescore(candidates)

    # Convenience wrapper so callers can persist without knowing the store.
    def research_result_dict(self, result: ResearchResult) -> dict[str, Any]:
        return {
            "email": result.email,
            "email_hash": result.email_hash,
            "domain": result.domain,
            "verdict": result.verdict.value,
            "candidates": [c.to_dict() for c in result.candidates],
            "sources_checked": result.sources_checked,
            "source_errors": result.source_errors,
            "attempts": result.attempts,
        }


class ResearchApplier:
    """Integration boundary: writes attribution into a lead record.

    Conservative (spec §9): a lead is only modified when the verdict is
    ``attributed``. Any other verdict leaves the lead untouched, so an
    unresolved/ambiguous email is never silently assigned a person.
    """

    def apply(self, lead: dict[str, Any], result: ResearchResult) -> dict[str, Any]:
        if result.verdict is not AttributionVerdict.attributed:
            return lead
        person = result.bound_candidate
        if person is None:
            return lead
        out = dict(lead)
        ph = dict(lead.get("plan_holder") or {})
        ph["person"] = {
            "name": person.name,
            "role": person.role,
            "source": "person_research_v1",
        }
        emails = [dict(e) for e in (ph.get("emails") or [])]
        for e in emails:
            if _same_email(e.get("email"), result.email):
                e["tier"] = "person_bound"
                e["person_name"] = person.name
        ph["emails"] = emails
        out["plan_holder"] = ph
        return out


def _triage_reason(email: str) -> str:
    """Return a reason string if the email has no researchable person, else ""."""
    if not email:
        return "empty_email"
    if is_free_mail_domain(email):
        return "free_mail_domain"
    local = email.rsplit("@", 1)[0] if "@" in email else email
    if is_generic_email_local_part(local):
        return "generic_local_part"
    return ""


def _corroborate(
    indexed: IndexedWebSource, facts: DomainFacts, target: str
) -> dict[str, list[ResearchEvidence]]:
    local = target.rsplit("@", 1)[0] if target else ""
    domain = target.rsplit("@", 1)[1] if "@" in target else ""
    names = {c.get("name") for c in facts.email_contexts.get(target, []) if c.get("name")}
    out: dict[str, list[ResearchEvidence]] = {}
    for name in names:
        if not name:
            continue
        out[name.lower()] = indexed.corroborate(target, domain, name)
    return out


def _same_email(a: str | None, b: str | None) -> bool:
    try:
        return bool(a) and bool(b) and normalize_email(a) == normalize_email(b)
    except Exception:  # noqa: BLE001
        return False
