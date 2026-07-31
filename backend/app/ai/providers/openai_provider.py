"""OpenAI-compatible chat provider."""

import logging

import openai

from app.ai.base import BaseAIProvider
from app.core.config import settings

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseAIProvider):
    """Wrap the OpenAI (or compatible) chat completion API."""

    def __init__(self) -> None:
        self._client = openai.OpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        )
        self._model = settings.OPENAI_MODEL

    def generate(self, prompt: str) -> str:
        """Call the OpenAI chat completion endpoint.

        Args:
            prompt: User prompt.

        Returns:
            The assistant message text.
        """
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""
