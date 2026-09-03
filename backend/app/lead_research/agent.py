"""AI Lead Research — Agent (pipeline orchestration).

Ties all stages into one pipeline:

  Stage 0  triage          → skip free-mail / generic local-part
  Stage 1  refine/company  → CompanyResearcher (AI cited)
  Stage 2  person          → PersonResearcherAI (deterministic + AI augment)
  Stage 3  intent/timing   → IntentTimingAnalyzer (AI reasoned)
  Stage 4  fit/score       → LeadScorer (AI + deterministic gate)

All seams are injectable for testing.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.lead_research.company_research import CompanyResearcher
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import LeadDossier
from app.lead_research.person_research_ai import PersonResearcherAI
from app.lead_research.scoring import LeadScorer

logger = logging.getLogger(__name__)


def _is_free_mail(domain: str) -> bool:
    """Check if domain is a free email provider."""
    free_mail = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "aol.com", "icloud.com", "mail.com", "protonmail.com",
        "zoho.com", "yandex.com", "gmx.com", "live.com",
    }
    return domain.lower() in free_mail


def _is_generic_local_part(email: str) -> bool:
    """Check if email local-part is generic (info@, office@, etc.)."""
    local = email.split("@")[0].lower()
    generic = {
        "info", "office", "admin", "contact", "hello", "support",
        "sales", "help", "team", "mail", "email", "webmaster",
        "postmaster", "abuse", "noreply", "no-reply",
    }
    return local in generic


class AILeadResearchAgent:
    """Pipeline orchestration for researching one email+domain."""

    def __init__(
        self,
        *,
        company_researcher: CompanyResearcher | None = None,
        person_researcher: PersonResearcherAI | None = None,
        intent_analyzer: IntentTimingAnalyzer | None = None,
        scorer: LeadScorer | None = None,
    ) -> None:
        self._company = company_researcher or CompanyResearcher()
        self._person = person_researcher or PersonResearcherAI()
        self._intent = intent_analyzer or IntentTimingAnalyzer()
        self._scorer = scorer or LeadScorer()

    def research(self, email: str, domain: str) -> LeadDossier:
        """Run the full pipeline for one email+domain.

        Returns a complete LeadDossier.
        """
        dossier = LeadDossier(email=email, domain=domain)
        sources_checked: list[str] = []
        source_errors: dict[str, str] = {}

        # Stage 0: triage
        if _is_free_mail(domain):
            dossier.fit = "Skipped — free mail domain"
            dossier.recommendation = "skip"
            logger.info("Triage: %s is free mail → skip", domain)
            return dossier

        if _is_generic_local_part(email):
            dossier.fit = "Skipped — generic local part"
            dossier.recommendation = "skip"
            logger.info("Triage: %s has generic local part → skip", email)
            return dossier

        # Stage 1: company research
        try:
            company_result = self._company.research_with_domain(email, domain)
            dossier.refined_domain = company_result.get("refined_domain", domain)
            dossier.refined_company = company_result.get("name", "")

            from app.lead_research.models import CompanyProfile, AIEvidence
            dossier.company = CompanyProfile(
                name=company_result.get("name", ""),
                industry=company_result.get("industry", ""),
                location=company_result.get("location", ""),
                website=company_result.get("website", ""),
                facts=[AIEvidence.from_dict(f) for f in company_result.get("facts", [])],
            )
            sources_checked.append("company_research")
        except Exception as exc:
            logger.error("Stage 1 failed for %s: %s", email, exc)
            source_errors["company_research"] = str(exc)
            dossier.refined_domain = domain

        # Stage 2: person research
        try:
            person_result = self._person.research(
                email=email,
                refined_domain=dossier.refined_domain or domain,
                company_name=dossier.company.name,
                company_industry=dossier.company.industry,
            )
            dossier.person = person_result
            sources_checked.append("person_research")
        except Exception as exc:
            logger.error("Stage 2 failed for %s: %s", email, exc)
            source_errors["person_research"] = str(exc)

        # Stage 3: intent + timing
        try:
            intent, timing = self._intent.analyze(
                email=email,
                company=dossier.company,
                person=dossier.person,
            )
            dossier.intent = intent
            dossier.timing = timing
            sources_checked.append("intent_timing")
        except Exception as exc:
            logger.error("Stage 3 failed for %s: %s", email, exc)
            source_errors["intent_timing"] = str(exc)

        # Stage 4: scoring
        try:
            fit, score, rec = self._scorer.score(
                email=email,
                company=dossier.company,
                person=dossier.person,
                intent=dossier.intent,
                timing=dossier.timing,
            )
            dossier.fit = fit
            dossier.potential_score = score
            dossier.recommendation = rec
            sources_checked.append("scoring")
        except Exception as exc:
            logger.error("Stage 4 failed for %s: %s", email, exc)
            source_errors["scoring"] = str(exc)

        dossier.sources_checked = sources_checked
        dossier.source_errors = source_errors

        logger.info(
            "Research complete: %s → score=%.1f rec=%s",
            email, dossier.potential_score, dossier.recommendation,
        )
        return dossier
