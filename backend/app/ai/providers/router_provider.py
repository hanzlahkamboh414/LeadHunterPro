"""Generic OpenAI-compatible AI provider — the single AI transport.

Practically every LLM vendor and router now exposes an OpenAI-compatible
``/chat/completions`` endpoint: NaraRouter, OmniRoute, OpenAI itself,
OpenRouter, Groq, Together, DeepSeek, vLLM, and Ollama's compatibility layer.
One provider class therefore covers all of them, which is why this module
replaced the four near-identical vendor providers that came before it
(CLAUDE.md section 14: one implementation, not five copies).

Switching vendor is a CONFIGURATION change, never a code change. Edit three
lines in ``backend/.env`` and nothing else in the repository moves::

    AI_BASE_URL=...     # the vendor's OpenAI-compatible base URL
    AI_MODEL=...        # exact model id as that vendor spells it
    AI_API_KEY=...      # the vendor's key

That is the whole switch procedure. ``backend/.env`` carries ready-made presets
for OpenAI, OmniRoute and Ollama as commented blocks.

Live-proven 2026-08-19 against NaraRouter (``agnes-2.0-flash`` -> HTTP 200)
through ``scripts/smoke_ai.py``, which drives the real ``AIGateway`` rather
than a raw client, so the abstraction itself is what was validated.

Credentials come from settings only, so no secret is hardcoded, logged, or
committed. Errors propagate untouched: a dead transport must be loud, never
silently degraded into a fabricated answer (CLAUDE.md section 12).
"""

import logging

import openai

from app.ai.base import BaseAIProvider
from app.ai.providers.registry import register_ai_provider
from app.core.config import settings

logger = logging.getLogger(__name__)


@register_ai_provider("router")
class RouterProvider(BaseAIProvider):
    """Call any OpenAI-compatible chat completion endpoint.

    The endpoint, model and credential are read from settings at construction
    time, so the class carries no vendor-specific knowledge at all.
    """

    def __init__(self, api_key: str | None = None) -> None:
        # Allow a specific key to be injected (e.g. the second key for deep
        # research). Falls back to the primary configured key.
        key = api_key if api_key else settings.AI_API_KEY
        self._client = openai.OpenAI(
            api_key=key,
            base_url=settings.AI_BASE_URL,
        )
        self._model = settings.AI_MODEL
        logger.debug(
            "RouterProvider ready: base_url=%s model=%s (key length %d, value never logged)",
            settings.AI_BASE_URL,
            self._model,
            len(key),
        )

    def generate(self, prompt: str) -> str:
        """Send *prompt* to the configured endpoint and return the reply text.

        Args:
            prompt: User prompt.

        Returns:
            The assistant message text; ``""`` when the vendor returns empty
            content, which keeps the response contract the rest of the
            application already expects.
        """
        # No max_tokens is sent, and that is deliberate. The now-deleted
        # anthropic_provider.py had to pass max_tokens=4096 because the
        # Anthropic Messages API makes the field MANDATORY -- that value was
        # picked to leave a reasoning model's JSON room above its thinking
        # block, not to add a safeguard. On the OpenAI-compatible protocol the
        # field is optional, and omitting it means "no client-imposed cap", so
        # hardcoding a number here would INTRODUCE a truncation limit that does
        # not exist today. The scorer's reply is a JSON object that must arrive
        # whole, so the uncapped form is the safer one.
        #
        # The residual risk is therefore the vendor's own default cap, not this
        # code. If a scored run ever yields truncated JSON, check that default
        # first and set the cap in configuration -- never silently shorten the
        # prompt or accept a partial object (CLAUDE.md section 12).
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""
