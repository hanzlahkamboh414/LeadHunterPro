"""Anthropic Claude provider."""

import logging

import anthropic

from app.ai.base import BaseAIProvider
from app.core.config import settings

logger = logging.getLogger(__name__)


class AnthropicProvider(BaseAIProvider):
    """Wrap the Anthropic Messages API."""

    def __init__(self) -> None:
        self._client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY or "local-omniroute",
            base_url=settings.ANTHROPIC_BASE_URL,
        )
        self._model = settings.ANTHROPIC_MODEL

    def generate(self, prompt: str) -> str:
        """Call the Anthropic Messages API.

        Args:
            prompt: User prompt.

        Returns:
            The generated text response.
        """
        message = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        )

        # Combo-LeadHunter returns [ThinkingBlock, TextBlock]. Thinking blocks
        # are skipped; ALL text blocks are joined so a JSON payload split
        # across multiple text blocks is returned in full. max_tokens covers
        # thinking + text output, so 1024 could truncate the JSON mid-object;
        # 4096 gives the JSON headroom above the thinking block.
        text_blocks = [
            block.text
            for block in message.content
            if getattr(block, "type", None) == "text"
        ]
        if not text_blocks:
            raise RuntimeError("Anthropic response contained no text block.")
        return "".join(text_blocks)