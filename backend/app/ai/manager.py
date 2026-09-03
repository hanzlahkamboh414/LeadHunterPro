"""AI manager — resolves the configured LLM provider from the registry."""

import logging

from app.ai.base import BaseAIProvider
from app.ai.providers.registry import resolve
from app.core.config import settings

logger = logging.getLogger(__name__)


class AIManager:
    """Factory that returns the configured :class:`BaseAIProvider`.

    Provider selection is *data*, not code: the name comes from
    ``settings.AI_PROVIDER`` and the class comes from the provider registry.
    Adding a provider therefore never edits this file — see
    :mod:`app.ai.providers.registry` for the three-step procedure.

    A provider name that is not registered RAISES. It must never fall back to a
    stub, because a stub that echoes the prompt makes a dead AI look like
    "0 qualified leads" instead of an error, which is exactly the fake-live-mode
    failure CLAUDE.md section 12 forbids.
    """

    def __init__(self) -> None:
        self._provider = self._resolve_provider()

    def _resolve_provider(self) -> BaseAIProvider:
        """Resolve the provider named by ``AI_PROVIDER``.

        Returns:
            The instantiated provider.

        Raises:
            AIProviderNotFoundError: When ``AI_PROVIDER`` is not registered.
        """
        name = settings.AI_PROVIDER
        provider = resolve(name)
        logger.info(
            "AI provider resolved: AI_PROVIDER=%r -> %s (model=%s)",
            name,
            type(provider).__name__,
            settings.AI_MODEL,
        )
        return provider

    def generate(self, prompt: str) -> str:
        """Delegate generation to the resolved provider.

        Args:
            prompt: Input prompt.

        Returns:
            Provider response text.
        """
        return self._provider.generate(prompt)
