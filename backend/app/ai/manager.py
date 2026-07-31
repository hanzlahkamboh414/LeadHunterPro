"""AI manager — selects and instantiates the configured LLM provider."""

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


class AIManager:
    """Factory that returns the configured :class:`BaseAIProvider`."""

    def __init__(self) -> None:
        self._provider = self._resolve_provider()

    def _resolve_provider(self) -> object:
        """Import and instantiate the provider named in settings."""
        provider_name = settings.AI_PROVIDER.lower()
        if provider_name == "openai":
            from app.ai.providers.openai_provider import OpenAIProvider  # noqa: PLC0415
            return OpenAIProvider()
        if provider_name == "anthropic":
            from app.ai.providers.anthropic_provider import AnthropicProvider  # noqa: PLC0415
            return AnthropicProvider()
        if provider_name == "gemini":
            from app.ai.providers.gemini_provider import GeminiProvider  # noqa: PLC0415
            return GeminiProvider()
        from app.ai.providers.local_provider import LocalProvider  # noqa: PLC0415
        logger.warning("Unknown AI_PROVIDER=%r; falling back to LocalProvider", provider_name)
        return LocalProvider()

    def generate(self, prompt: str) -> str:
        """Delegate generation to the resolved provider.

        Args:
            prompt: Input prompt.

        Returns:
            Provider response text.
        """
        return self._provider.generate(prompt)  # type: ignore[return-value]
