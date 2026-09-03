"""AI module – prompts, providers, and orchestration layer."""

from app.ai.base import BaseAIProvider
from app.ai.gateway import AIGateway
from app.ai.manager import AIManager
from app.ai.prompts.company_summary import build_company_summary_prompt
from app.ai.providers.registry import (
    AIProviderNotFoundError,
    get_provider_names,
    register_ai_provider,
)
from app.ai.scorer import CompanyScorer
from app.ai.summarizer import CompanySummarizer

# Provider classes are deliberately NOT re-exported here: they are resolved by
# name through the registry, so nothing has to import a vendor module directly.
__all__ = [
    "AIGateway",
    "AIManager",
    "AIProviderNotFoundError",
    "BaseAIProvider",
    "CompanyScorer",
    "CompanySummarizer",
    "build_company_summary_prompt",
    "get_provider_names",
    "register_ai_provider",
]
