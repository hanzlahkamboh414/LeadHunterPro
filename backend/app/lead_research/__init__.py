"""AI Lead Research Agent — V1.

Additive side-band that takes a single company email+domain (e.g. from a
plan-holder PDF with ``person=None``) and produces a **LeadDossier**:
refined company, attributed person, buying-intent, timing, fit for
The Best Estimator LLC, and a potential-lead score — all with source-cited
facts and reasoned analysis.

Builds on (reuses, never edits) ``app/person_research``, the AI RouterProvider,
the search-provider registry, and the deterministic gate.

See ``docs/lead_research_v1_spec.md`` for the locked spec.
"""

from app.lead_research.agent import AILeadResearchAgent
from app.lead_research.company_research import CompanyResearcher, default_refine_domain
from app.lead_research.intent_timing import IntentTimingAnalyzer
from app.lead_research.models import (
    AIEvidence,
    CompanyProfile,
    IntentAssessment,
    LeadDossier,
    PersonFindings,
    TimingAssessment,
)
from app.lead_research.person_research_ai import PersonResearcherAI
from app.lead_research.scoring import LeadScorer
from app.lead_research.service import LeadResearchService, LeadResearchStore

__all__ = [
    "AIEvidence",
    "AILeadResearchAgent",
    "CompanyProfile",
    "CompanyResearcher",
    "IntentAssessment",
    "IntentTimingAnalyzer",
    "LeadDossier",
    "LeadScorer",
    "LeadResearchService",
    "LeadResearchStore",
    "PersonFindings",
    "PersonResearcherAI",
    "TimingAssessment",
    "default_refine_domain",
]
