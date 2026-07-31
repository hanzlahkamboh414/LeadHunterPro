"""Google Gemini provider."""

import logging

import google.generativeai as genai

from app.ai.base import BaseAIProvider
from app.core.config import settings

logger = logging.getLogger(__name__)


class GeminiProvider(BaseAIProvider):
    """Wrap the Google Gemini generative AI API."""

    def __init__(self) -> None:
        genai.configure(api_key=settings.OPENAI_API_KEY)  # reuses key env var for now
        self._model = genai.GenerativeModel("gemini-pro")

    def generate(self, prompt: str) -> str:
        """Call the Gemini generate_content endpoint.

        Args:
            prompt: User prompt.

        Returns:
            The model response text.
        """
        response = self._model.generate_content(prompt)
        return response.text
