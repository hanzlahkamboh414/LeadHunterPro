"""Anthropic Claude provider."""

import logging

import anthropic

from app.ai.base import BaseAIProvider
from app.core.config import settings

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseAIProvider):
    """Wrap the Anthropic Messages API."""

    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=settings.OPENAI_API_KEY)  # reuses OPENAI_API_KEY env for now
        self._model = "claude-sonnet-4-20250514"

    def generate(self, prompt: str) -> str:
        """Call the Anthropic Messages API.

        Args:
            prompt: User prompt.

        Returns:
            The assistant message text.
        """
        message = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
