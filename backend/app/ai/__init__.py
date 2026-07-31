"""AI module – prompts, providers, and orchestration layer."""

from app.ai.base import BaseAIProvider
from app.ai.gateway import AIGateway
from app.ai.manager import AIManager
from app.ai.prompts.company_summary import build_company_summary_prompt
from app.ai.providers.local_provider import LocalProvider
from app.ai.scorer import CompanyScorer
from app.ai.summarizer import CompanySummarizer

__all__ = [
    "AIGateway",
    "AIManager",
    "BaseAIProvider",
    "CompanyScorer",
    "CompanySummarizer",
    "LocalProvider",
    "build_company_summary_prompt",
]
