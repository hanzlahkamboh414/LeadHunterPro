"""Increment7 — LeadPipeline orchestration: verified company -> qualified Lead.

Assembles every existing stage into one honest pipeline (no new discovery
engine — reuse only, rule #14):

    CompanyDiscoveryResult (gate_accepted, from the discovery/verification path)
        -> LeadershipDiscovery        -> decision-maker person + bound emails
        -> verify_email_domains       (already applied inside leadership discovery)
        -> intent plugins (capability-isolated) -> IntentEvidence[]
        -> CompanyScorer.qualify      -> LeadAI (ai_confidence + justification)
        -> Lead + deterministic qualification_gate (the verdict)

Every network stage is injected behind a seam so tests run fully offline:
``leadership`` (any object with ``discover(website)``), ``intent_plugins``
(any object with ``collect_evidence(...)``), ``scorer`` (any object with
``qualify(company, query)``). A stage failing is never fatal to the assembly —
the Lead is still built and the gate reports exactly what is missing, so a
partial lead is surfaced honestly instead of silently passing or crashing.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.ai.scorer import CompanyScorer
from app.discovery.intent import (
    CompanySiteIntentPlugin,
    GoogleNewsPlugin,
    USAspendingPlugin,
)
from app.discovery.leadership_discovery import LeadershipDiscovery
from app.engines.discovery.company.company_models import CompanyDiscoveryResult
from app.engines.lead.lead_models import (
    EmailVerificationTier,
    IntentEvidence,
    Lead,
    LeadAI,
    LeadEmail,
    LeadPerson,
    PersonVerificationTier,
)

logger = logging.getLogger(__name__)


class LeadershipProvider(Protocol):
    """Any source of leadership records (``discover`` -> list of person dicts)."""

    def discover(self, website: str) -> list[dict[str, Any]]: ...


class IntentProvider(Protocol):
    """Any buying-intent plugin (matches ``BaseIntentPlugin.collect_evidence``)."""

    def collect_evidence(
        self,
        *,
        company_name: str,
        website: str = "",
        location: str = "",
    ) -> tuple[Any, list[IntentEvidence], dict[str, Any]]: ...


class ScorerProvider(Protocol):
    """Any scorer exposing ``qualify(company, query)`` (CompanyScorer fits)."""

    def qualify(
        self, company: dict[str, Any], query: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


def default_intent_plugins() -> list[IntentProvider]:
    """The three free/keyless intent plugins, constructed offline-safe."""
    return [
        CompanySiteIntentPlugin(),
        GoogleNewsPlugin(),
        USAspendingPlugin(),
    ]


class LeadPipeline:
    """Compose a verified company into a qualified :class:`Lead`."""

    def __init__(
        self,
        *,
        leadership: LeadershipProvider | None = None,
        intent_plugins: list[IntentProvider] | None = None,
        scorer: ScorerProvider | None = None,
    ) -> None:
        self._leadership = (
            leadership if leadership is not None else LeadershipDiscovery()
        )
        self._intent_plugins = (
            intent_plugins if intent_plugins is not None else default_intent_plugins()
        )
        self._scorer = scorer if scorer is not None else CompanyScorer(use_ai=True)

    def qualify_company(
        self,
        company: CompanyDiscoveryResult,
        query: dict[str, Any] | None = None,
    ) -> Lead:
        """Assemble and return the Lead; the deterministic gate is the verdict."""
        person, emails = self._discover_person(company.website)
        evidence = self._collect_intent(company, query)
        ai = self._score(company, evidence, emails, query)
        return Lead(
            company=company,
            person=person,
            emails=emails,
            intent_evidence=evidence,
            ai=ai,
        )

    def qualify_many(
        self,
        companies: list[CompanyDiscoveryResult],
        query: dict[str, Any] | None = None,
    ) -> list[Lead]:
        """Qualify a batch of companies, one Lead each (order preserved)."""
        return [self.qualify_company(c, query) for c in companies]

    # -- stages ----------------------------------------------------------

    def _discover_person(
        self, website: str
    ) -> tuple[LeadPerson | None, list[LeadEmail]]:
        try:
            records = self._leadership.discover(website)
        except Exception as exc:  # noqa: BLE001 — a failed discovery never kills the run
            logger.warning("leadership discovery failed for %s: %s", website, exc)
            return None, []
        return self._select_decision_maker(records)

    def _collect_intent(
        self, company: CompanyDiscoveryResult, query: dict[str, Any] | None
    ) -> list[IntentEvidence]:
        location = str((query or {}).get("location") or "")
        evidence: list[IntentEvidence] = []
        seen: set[tuple[str, str]] = set()
        for plugin in self._intent_plugins:
            try:
                _, items, _ = plugin.collect_evidence(
                    company_name=company.company_name,
                    website=company.website,
                    location=location,
                )
            except Exception as exc:  # noqa: BLE001 — one bad plugin never kills the run
                logger.warning(
                    "intent plugin %r failed: %s",
                    getattr(plugin, "name", type(plugin).__name__),
                    exc,
                )
                continue
            for item in items:
                key = (item.type.value, item.source_url)
                if item.source_url.strip() and key not in seen:
                    seen.add(key)
                    evidence.append(item)
        return evidence

    def _score(
        self,
        company: CompanyDiscoveryResult,
        evidence: list[IntentEvidence],
        emails: list[LeadEmail],
        query: dict[str, Any] | None,
    ) -> LeadAI:
        data = self._company_dict(company, evidence, emails)
        try:
            result = self._scorer.qualify(data, query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("scoring failed for %s: %s", company.company_name, exc)
            return LeadAI(error=f"{type(exc).__name__}: {exc}")
        return self._lead_ai_from(result)

    # -- helpers ---------------------------------------------------------

    def _company_dict(
        self,
        company: CompanyDiscoveryResult,
        evidence: list[IntentEvidence],
        emails: list[LeadEmail],
    ) -> dict[str, Any]:
        """The evidence dict the scorer/prompt consume (demo stage_scoring shape)."""
        meta = company.metadata or {}
        return {
            "title": company.company_name,
            "company_name": company.company_name,
            "website": company.website,
            "city": company.city,
            "state": company.state,
            "trade_category": meta.get("trade_category", ""),
            "industry_focus": meta.get("industry_focus", ""),
            "description": (
                meta.get("industry_focus")
                or meta.get("trade_category")
                or company.discovery_reason
                or ""
            ),
            "source": company.source,
            "source_url": company.source_url,
            "emails": [e.email for e in emails],
            "intent_evidence": [e.to_dict() for e in evidence],
        }

    @staticmethod
    def _select_decision_maker(
        records: list[dict[str, Any]],
    ) -> tuple[LeadPerson | None, list[LeadEmail]]:
        """Best person: role_relevant preferred, person_bound email preferred."""
        if not records:
            return None, []
        relevant = [r for r in records if (r.get("person") or {}).get("role_relevance")]
        pool = relevant if relevant else records
        best = pool[0]
        for candidate in pool:
            if any(
                e.get("tier") == "person_bound"
                for e in candidate.get("emails") or []
            ):
                best = candidate
                break
        person_dict = best.get("person") or {}
        person = LeadPerson(
            name=str(person_dict.get("name") or ""),
            role=str(person_dict.get("role") or ""),
            role_relevance=bool(person_dict.get("role_relevance")),
            tier=PersonVerificationTier(person_dict.get("tier") or "unverified"),
            source_url=str(person_dict.get("source_url") or ""),
        )
        emails = [
            LeadEmail(
                email=str(e.get("email") or ""),
                tier=EmailVerificationTier(e.get("tier") or "format"),
                source_url=str(e.get("source_url") or ""),
            )
            for e in best.get("emails") or []
            if e.get("email")
        ]
        return person, emails

    @staticmethod
    def _lead_ai_from(result: dict[str, Any]) -> LeadAI:
        """Map CompanyScorer.qualify output onto LeadAI (fields mirror verbatim)."""
        return LeadAI(
            ai_confidence=float(result.get("ai_confidence") or 0.0),
            justification=str(result.get("justification") or ""),
            qualified=result.get("qualified"),
            ai_used=bool(result.get("ai_used")),
            deterministic_score=float(result.get("deterministic_score") or 0.0),
            ai_score=result.get("ai_score"),
            reasons=list(result.get("reasons") or []),
            strengths=list(result.get("strengths") or []),
            concerns=list(result.get("concerns") or []),
            error=result.get("error"),
        )


def lead_to_export_row(lead: Lead) -> dict[str, Any]:
    """A demo.py-export-shaped row for a Lead (reuses the export contract).

    Mirrors ``demo.EXPORT_FIELDS`` (company_name, website, city, state, ...)
    and adds the V1 lead-qualification fields the gate produces. A consumer
    (demo.py / API) can reuse this row verbatim for JSON or Excel output.
    """
    meta = lead.company.metadata or {}
    return {
        "company_name": lead.company.company_name,
        "website": lead.company.website,
        "city": lead.company.city,
        "state": lead.company.state,
        "industry": (
            meta.get("industry_focus")
            or meta.get("trade_category")
            or lead.company.discovery_reason
            or ""
        ),
        "source": lead.company.source,
        "source_url": lead.company.source_url,
        "gate_accepted": lead.verified_context,
        "decision_maker": (lead.person.name if lead.person else ""),
        "decision_maker_role": (lead.person.role if lead.person else ""),
        "person_bound_email": (
            lead.emails[0].email if lead.has_person_bound_email and lead.emails else ""
        ),
        "intent_evidence": [e.source_url for e in lead.intent_evidence],
        "ai_confidence": lead.ai.ai_confidence,
        "justification": lead.ai.justification,
        "deterministic_score": lead.ai.deterministic_score,
        "ai_score": lead.ai.ai_score if lead.ai.ai_score is not None else "",
        "ai_used": lead.ai.ai_used,
        "qualified": lead.qualifies,
        "blocked_by": lead.qualification_gate().blocked_by,
        "created_at": lead.created_at,
    }
