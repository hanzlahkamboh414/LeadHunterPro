"""Gateway that routes AI requests to the configured provider."""

import logging
from typing import Callable

from app.ai.manager import AIManager
from app.core.config import settings

logger = logging.getLogger(__name__)


class AIGateway:
    """Route prompts to the active AI provider."""

    def __init__(self) -> None:
        self._manager = AIManager()

    def ask(self, prompt: str) -> str:
        """Send *prompt* to the AI provider and return the response.

        Args:
            prompt: The user prompt.

        Returns:
            The provider's text response.
        """
        return self._manager.generate(prompt)


def make_ai_ask(api_key: str | None = None) -> Callable[[str], str]:
    """Build an ``ask(prompt) -> str`` callable on an arbitrary API key.

    Used to run a second, parallel AI lane on a separate key (``AI_API_KEY_2``)
    so the main pipeline and the deep-research stage do not share a rate limit.

    Args:
        api_key: Override key. Falls back to ``AI_API_KEY_2``, then the
            primary ``AI_API_KEY``.

    Returns:
        A callable matching ``AIGateway.ask``'s signature.
    """
    from app.ai.providers.registry import get_provider_class

    key = api_key or settings.AI_API_KEY_2 or settings.AI_API_KEY
    cls = get_provider_class(settings.AI_PROVIDER)
    if cls is None:
        raise ValueError(f"AI provider {settings.AI_PROVIDER!r} is not registered")
    provider = cls(api_key=key)
    return provider.generate
