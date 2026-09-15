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


def make_ai_ask(
    api_key: str | None = None, model: str | None = None
) -> Callable[[str], str]:
    """Build an ``ask(prompt) -> str`` callable on an arbitrary API key/model.

    Used to run a second, parallel AI lane on a separate key (``AI_API_KEY_2``)
    so the main pipeline and the deep-research stage do not share a rate limit.
    Every current caller is a deep-thinking lane (deep-research, discovery
    query expansion, Phase H template generation, source-scout), so the default
    model here is ``AI_MODEL_DEEP`` (agnes-3-flash: clean JSON, slower) while
    the main pipeline stays on the faster ``AI_MODEL``.

    Args:
        api_key: Override key. Falls back to ``AI_API_KEY_2``, then the
            primary ``AI_API_KEY``.
        model: Override model. Falls back to ``AI_MODEL_DEEP``, then
            ``AI_MODEL``.

    Returns:
        A callable matching ``AIGateway.ask``'s signature.
    """
    from app.ai.providers.registry import get_provider_class

    key = api_key or settings.AI_API_KEY_2 or settings.AI_API_KEY
    resolved_model = model or settings.AI_MODEL_DEEP or settings.AI_MODEL
    cls = get_provider_class(settings.AI_PROVIDER)
    if cls is None:
        raise ValueError(f"AI provider {settings.AI_PROVIDER!r} is not registered")
    provider = cls(api_key=key, model=resolved_model)
    return provider.generate
