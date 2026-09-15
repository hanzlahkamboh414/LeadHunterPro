"""Offline unit tests for the generic OpenAI-compatible AI provider.

``RouterProvider`` is the single AI transport. It speaks the OpenAI-compatible
protocol that every major vendor and router exposes, so switching vendor is a
``backend/.env`` change and never a code change. It is wired in as a normal
``BaseAIProvider`` and resolved *by name* through the provider registry::

    AIGateway -> AIManager -> registry.resolve(AI_PROVIDER) -> RouterProvider

These tests NEVER call the network. ``openai.OpenAI`` is mocked everywhere so no
real client is ever constructed, and no API key is hardcoded, printed, or
committed. The only live check lives in ``backend/scripts/smoke_ai.py``.

Two guarantees here are structural rather than behavioural, and both exist to
stop old mistakes coming back (CLAUDE.md section 12):

* an unregistered ``AI_PROVIDER`` must RAISE, never degrade into a stub that
  echoes the prompt back and turns a dead AI into a silent "0 qualified leads";
* the deleted vendor-specific providers must stay unregistered.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest

from app.ai.base import BaseAIProvider
from app.ai.gateway import AIGateway, make_ai_ask
from app.ai.manager import AIManager
from app.ai.providers.registry import (
    AIProviderNotFoundError,
    get_provider_class,
    get_provider_names,
)
from app.ai.providers.router_provider import RouterProvider
from app.core.config import Settings, settings
from app.engines.ai_engine import AIEngine

# Patch target for the OpenAI-compatible client used inside RouterProvider.
_CLIENT = "app.ai.providers.router_provider.openai.OpenAI"

# Harmless, non-secret placeholders. Never a real key or a real endpoint.
_FAKE_KEY = "test-key-not-a-real-credential"
_FAKE_URL = "https://router.test.invalid/v1"
_FAKE_MODEL = "test-model-not-real"

# The model default that config.py declares. Recorded here so a silent change to
# the proven default fails a test instead of quietly changing AI behaviour.
# 2026-09-15 per-lane split: the MAIN pipeline is routine research and runs the
# fast 2.5 (3-7s/call); the deep-thinking lanes run AI_MODEL_DEEP below.
_PROVEN_DEFAULT_MODEL = "agnes-2.5-flash"

# The deep-lane model default (every make_ai_ask caller: deep-research,
# discovery query expansion, template generation, source-scout). agnes-3-flash
# returns CLEAN JSON (no markdown fences) but takes 25-40s/call, so it is
# confined to the low-volume deep lanes.
_PROVEN_DEEP_MODEL = "agnes-3-flash"

# The hard per-request AI timeout default. Changes to it are deliberate (they
# trade bounded latency against a slower/hung provider's completion), so a
# silent drift must fail this test rather than quietly re-blocking leads.
_PROVEN_DEFAULT_TIMEOUT = 60

# Vendor-specific providers that were deleted in favour of the generic one.
# None of these names may ever resolve again.
_REMOVED_PROVIDER_NAMES = ("openai", "anthropic", "gemini", "local", "nara")


def _fake_response(content: str | None) -> MagicMock:
    """Build a minimal OpenAI-compatible chat completion response."""
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _fake_request() -> httpx.Request:
    return httpx.Request("POST", _FAKE_URL)


@pytest.fixture
def patched_openai():
    """Patch ``openai.OpenAI`` so no real client or network call is made."""
    with patch(_CLIENT) as mock_client:
        yield mock_client


# ---------------------------------------------------------------------------
# Settings / configuration
# ---------------------------------------------------------------------------


def test_router_provider_is_a_base_provider() -> None:
    """RouterProvider implements the existing provider abstraction."""
    assert issubclass(RouterProvider, BaseAIProvider)
    assert callable(getattr(RouterProvider, "generate", None))


def test_settings_are_read_correctly(monkeypatch, patched_openai) -> None:
    """AI_API_KEY / AI_BASE_URL / AI_MODEL are wired from settings."""
    monkeypatch.setattr(settings, "AI_API_KEY", _FAKE_KEY)
    monkeypatch.setattr(settings, "AI_BASE_URL", _FAKE_URL)
    monkeypatch.setattr(settings, "AI_MODEL", _FAKE_MODEL)

    provider = RouterProvider()

    patched_openai.assert_called_once_with(
        api_key=settings.AI_API_KEY,
        base_url=settings.AI_BASE_URL,
        timeout=settings.AI_TIMEOUT_S,
    )
    assert provider._model == settings.AI_MODEL


def test_timeout_is_wired_from_settings(monkeypatch, patched_openai) -> None:
    """AI_TIMEOUT_S is forwarded to the OpenAI client as the hard per-request
    cap, so a slow router/model window fails a stage in bounded time instead of
    blocking the SDK's 600s default."""
    monkeypatch.setattr(settings, "AI_TIMEOUT_S", 42)
    RouterProvider()
    _, kwargs = patched_openai.call_args
    assert kwargs["timeout"] == 42


def test_declared_default_timeout_is_the_bounded_value() -> None:
    """config.py's AI_TIMEOUT_S default must stay a bounded cap (the SDK's
    default is 600s, which made a congested router block every lead ~10 min)."""
    declared = Settings.model_fields["AI_TIMEOUT_S"].default
    assert declared == _PROVEN_DEFAULT_TIMEOUT
    assert declared < 600


def test_base_url_is_used(monkeypatch, patched_openai) -> None:
    """AI_BASE_URL is forwarded to the OpenAI-compatible client verbatim.

    This is what makes a vendor switch a config-only change: any base URL the
    operator writes in ``.env`` is what the client actually talks to.
    """
    monkeypatch.setattr(settings, "AI_BASE_URL", _FAKE_URL)
    monkeypatch.setattr(settings, "AI_MODEL", _FAKE_MODEL)
    RouterProvider()
    _, kwargs = patched_openai.call_args
    assert kwargs["base_url"] == settings.AI_BASE_URL == _FAKE_URL


def test_api_key_comes_from_settings(monkeypatch, patched_openai) -> None:
    """The API key is sourced from settings, never a hardcoded literal."""
    monkeypatch.setattr(settings, "AI_API_KEY", _FAKE_KEY)
    monkeypatch.setattr(settings, "AI_MODEL", _FAKE_MODEL)
    RouterProvider()
    _, kwargs = patched_openai.call_args
    assert kwargs["api_key"] == settings.AI_API_KEY == _FAKE_KEY


def test_api_key_not_hardcoded(monkeypatch, patched_openai) -> None:
    """With an empty configured key, the provider still passes settings through
    rather than baking in a literal. The real client would reject an empty key,
    which is the correct loud failure."""
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    RouterProvider()
    _, kwargs = patched_openai.call_args
    assert kwargs["api_key"] == ""  # sourced from settings, not a hardcoded value


def test_declared_default_model_is_the_proven_one() -> None:
    """config.py's declared AI_MODEL default is the live-proven model.

    Asserted against the field default on the Settings class, not against the
    loaded ``settings`` object, because the loaded value comes from ``.env`` and
    would make this test tautological.
    """
    assert Settings.model_fields["AI_MODEL"].default == _PROVEN_DEFAULT_MODEL


def test_declared_default_provider_is_the_generic_router() -> None:
    """A checkout with no .env must still name a provider that exists."""
    declared = Settings.model_fields["AI_PROVIDER"].default
    assert declared == "router"
    assert get_provider_class(declared) is RouterProvider


def test_model_override(monkeypatch, patched_openai) -> None:
    """AI_MODEL is honoured, so swapping models needs no code change."""
    monkeypatch.setattr(settings, "AI_MODEL", _FAKE_MODEL)
    assert RouterProvider()._model == _FAKE_MODEL

    monkeypatch.setattr(settings, "AI_MODEL", "configured-override")
    assert RouterProvider()._model == "configured-override"


def test_declared_default_deep_model_is_the_proven_one() -> None:
    """config.py's AI_MODEL_DEEP default is the deep-lane model (2026-09-15
    per-lane split). The deep lanes need clean JSON and tolerate 25-40s/call;
    a silent change here silently changes every deep lane at once."""
    assert Settings.model_fields["AI_MODEL_DEEP"].default == _PROVEN_DEEP_MODEL


def test_provider_accepts_explicit_model_override(monkeypatch, patched_openai) -> None:
    """An explicit model wins over AI_MODEL, without touching global settings.

    This is the seam make_ai_ask uses to put the deep lanes on agnes-3-flash
    while the main pipeline stays on AI_MODEL.
    """
    monkeypatch.setattr(settings, "AI_MODEL", "main-lane-model")
    provider = RouterProvider(model="deep-lane-model")
    assert provider._model == "deep-lane-model"


def test_make_ai_ask_uses_the_deep_model_by_default(monkeypatch, patched_openai) -> None:
    """make_ai_ask is the deep-lane constructor: its default model is
    AI_MODEL_DEEP (agnes-3-flash), NOT the main pipeline's AI_MODEL. Every
    caller (deep-research, discovery, source-scout) is a deep-thinking lane."""
    monkeypatch.setattr(settings, "AI_MODEL", "fast-main-model")
    monkeypatch.setattr(settings, "AI_MODEL_DEEP", "deep-thinking-model")

    ask = make_ai_ask()

    assert ask.__self__._model == "deep-thinking-model"


def test_make_ai_ask_model_falls_back_to_ai_model(monkeypatch, patched_openai) -> None:
    """An unset AI_MODEL_DEEP falls back to AI_MODEL, so an old .env without
    the new key keeps working unchanged (same degrade pattern as the key
    lanes)."""
    monkeypatch.setattr(settings, "AI_MODEL", "fast-main-model")
    monkeypatch.setattr(settings, "AI_MODEL_DEEP", "")

    ask = make_ai_ask()

    assert ask.__self__._model == "fast-main-model"


def test_make_ai_ask_explicit_model_wins(monkeypatch, patched_openai) -> None:
    """An explicit model argument overrides both AI_MODEL_DEEP and AI_MODEL."""
    monkeypatch.setattr(settings, "AI_MODEL", "fast-main-model")
    monkeypatch.setattr(settings, "AI_MODEL_DEEP", "deep-thinking-model")

    ask = make_ai_ask(model="explicit-model")

    assert ask.__self__._model == "explicit-model"


def test_make_ai_ask_key_fallback_unchanged(monkeypatch, patched_openai) -> None:
    """The key lane contract is untouched by the model split: an explicit key
    wins, then AI_API_KEY_2, then AI_API_KEY."""
    monkeypatch.setattr(settings, "AI_API_KEY", "primary-key")
    monkeypatch.setattr(settings, "AI_API_KEY_2", "second-key")
    monkeypatch.setattr(settings, "AI_MODEL_DEEP", "deep-thinking-model")

    make_ai_ask()
    _, kwargs_default = patched_openai.call_args
    assert kwargs_default["api_key"] == "second-key"

    make_ai_ask(api_key="explicit-key")

    _, kwargs_explicit = patched_openai.call_args
    assert kwargs_explicit["api_key"] == "explicit-key"


# ---------------------------------------------------------------------------
# Request / response mapping
# ---------------------------------------------------------------------------


def test_request_messages_model_and_response_mapping(monkeypatch, patched_openai) -> None:
    """generate() sends the configured model + user message, and maps the
    OpenAI-compatible response into the same string the rest of the app expects."""
    monkeypatch.setattr(settings, "AI_MODEL", _FAKE_MODEL)
    provider = RouterProvider()

    create = provider._client.chat.completions.create
    create.return_value = _fake_response("ROUTER_OK")

    out = provider.generate("Reply with exactly: ROUTER_OK")

    create.assert_called_once_with(
        model=_FAKE_MODEL,
        messages=[{"role": "user", "content": "Reply with exactly: ROUTER_OK"}],
    )
    assert out == "ROUTER_OK"


def test_empty_response_returns_empty_string(patched_openai) -> None:
    """An empty/missing content is normalized to '' so the response contract holds."""
    provider = RouterProvider()
    provider._client.chat.completions.create.return_value = _fake_response(None)
    assert provider.generate("hi") == ""


def test_malformed_response_propagates(patched_openai) -> None:
    """A malformed response is NOT silently fabricated; it propagates."""
    provider = RouterProvider()
    bad = MagicMock()
    bad.choices = []  # no choices -> provider raises, never returns fabricated text
    provider._client.chat.completions.create.return_value = bad
    with pytest.raises(IndexError):
        provider.generate("hi")


def test_returns_str_not_dict_cannot_overwrite_verification(patched_openai) -> None:
    """The provider returns a leaf string, so it can never collide with / overwrite
    deterministic verification fields (verification vs AI intelligence stay separate)."""
    provider = RouterProvider()
    provider._client.chat.completions.create.return_value = _fake_response("ROUTER_OK")
    assert isinstance(provider.generate("hi"), str)


# ---------------------------------------------------------------------------
# Error handling (provider convention: propagate, never swallow)
# ---------------------------------------------------------------------------


def test_401_403_errors_propagated(patched_openai) -> None:
    """Authentication errors (HTTP 401/403) propagate rather than being hidden."""
    provider = RouterProvider()
    create = provider._client.chat.completions.create
    for status, label in ((401, "unauthorized"), (403, "forbidden")):
        create.side_effect = openai.AuthenticationError(
            label,
            response=httpx.Response(status, request=_fake_request()),
            body={"error": {"message": label}},
        )
        with pytest.raises(openai.AuthenticationError):
            provider.generate("hi")
    create.side_effect = None


def test_429_rate_limit_propagated(patched_openai) -> None:
    """HTTP 429 rate-limit errors propagate per the existing provider convention.

    This matters at scale: bulk scoring must be able to see a rate limit and
    react, not receive a fabricated answer.
    """
    provider = RouterProvider()
    provider._client.chat.completions.create.side_effect = openai.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=_fake_request()),
        body={"error": {"message": "rate limited"}},
    )
    with pytest.raises(openai.RateLimitError):
        provider.generate("hi")


def test_timeout_and_connection_errors_propagated(patched_openai) -> None:
    """Timeout / connection failures propagate; no fabricated output is returned."""
    provider = RouterProvider()
    req = _fake_request()
    for exc in (openai.APITimeoutError(request=req), openai.APIConnectionError(request=req)):
        provider._client.chat.completions.create.side_effect = exc
        with pytest.raises(type(exc)):
            provider.generate("hi")
    provider._client.chat.completions.create.side_effect = None


# ---------------------------------------------------------------------------
# Registry: the extension seam (add a provider without editing old code)
# ---------------------------------------------------------------------------


def test_registry_autodiscovers_the_router_provider() -> None:
    """The registry finds providers by scanning the package, so adding a module
    is all that a new provider requires — no edit to manager.py or config.py."""
    assert "router" in get_provider_names()
    assert get_provider_class("router") is RouterProvider


def test_provider_name_lookup_is_case_insensitive() -> None:
    """AI_PROVIDER is operator-typed config, so casing must not matter."""
    assert get_provider_class("ROUTER") is RouterProvider
    assert get_provider_class("  Router  ") is RouterProvider


def test_unknown_provider_raises_instead_of_silent_stub(monkeypatch) -> None:
    """PROOF (CLAUDE.md section 12): a misconfigured AI_PROVIDER is FATAL.

    The old behaviour fell back to a stub that echoed the prompt back. The
    scorer could not parse that, swallowed the error, and reported a
    deterministic score — so a completely dead AI looked like "0 qualified
    leads" with no error anywhere. That must never be possible again.
    """
    monkeypatch.setattr(settings, "AI_PROVIDER", "provider-that-does-not-exist")
    with pytest.raises(AIProviderNotFoundError) as excinfo:
        AIManager()
    message = str(excinfo.value)
    # The error has to be actionable on its own, without reading the source.
    assert "provider-that-does-not-exist" in message
    assert "router" in message
    assert "backend/.env" in message


def test_removed_vendor_providers_stay_unregistered() -> None:
    """The deleted vendor-specific providers must never resolve again.

    Each was a liability: the OpenAI one duplicated this provider, the Anthropic
    one was structurally broken (its SDK appends /v1/messages to a base URL that
    already ended in /v1), the Gemini one read the wrong key variable, and the
    local one echoed prompts. Their capability is now covered by AI_BASE_URL.
    """
    for name in _REMOVED_PROVIDER_NAMES:
        assert get_provider_class(name) is None, f"{name!r} provider is registered again"


# ---------------------------------------------------------------------------
# AI boundary (AIGateway -> AIManager -> registry -> RouterProvider)
# ---------------------------------------------------------------------------


def test_ai_provider_router_selects_router_provider(monkeypatch, patched_openai) -> None:
    """AI_PROVIDER=router makes AIManager resolve to the generic provider."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "router")
    manager = AIManager()
    assert isinstance(manager._provider, RouterProvider)
    patched_openai.assert_called_once()


def test_gateway_routes_through_router_when_selected(monkeypatch, patched_openai) -> None:
    """End-to-end boundary: AIGateway.ask flows through RouterProvider to the
    (mocked) OpenAI-compatible client and back as a mapped string."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "router")
    patched_openai.return_value.chat.completions.create.return_value = _fake_response("ROUTER_OK")

    gateway = AIGateway()
    assert gateway.ask("Reply with exactly: ROUTER_OK") == "ROUTER_OK"


# ---------------------------------------------------------------------------
# Research/intelligence flow (PROOF: the real path reaches the configured router)
# ---------------------------------------------------------------------------

# Minimal but valid research inputs (mirrors tests/ai/test_lead_qualification.py).
_RESEARCH_DATA = {
    "title": "Dallas Roofing Co",
    "company_name": "Dallas Roofing Co",
    "website": "https://dallasroofing.example",
    "city": "Dallas",
    "state": "TX",
    "description": "Commercial roofing contractor serving Dallas, Texas",
    "emails": [],
    "phones": [],
}
_RESEARCH_QUERY = {"industry": "Roofing", "location": "Dallas Texas"}
_VALID_AI_JSON = (
    '{"score": 80, "qualified": true, "qualification": "strong fit", '
    '"reasons": ["industry relevance"], "strengths": ["local"], '
    '"concerns": []}'
)


def test_research_flow_reaches_the_configured_router(monkeypatch) -> None:
    """PROOF: the real research AI path reaches RouterProvider using exactly the
    endpoint and model configured in settings, and no other transport exists.

    Path exercised:
        AIEngine.qualify_lead
          -> CompanyScorer.qualify (app.ai.scorer, use_ai=True)
          -> AIGateway.ask
          -> AIManager -> registry.resolve("router")
          -> RouterProvider
          -> openai.OpenAI  (mocked; stands in for the configured endpoint)
    """
    monkeypatch.setattr(settings, "AI_PROVIDER", "router")
    monkeypatch.setattr(settings, "AI_API_KEY", _FAKE_KEY)
    monkeypatch.setattr(settings, "AI_BASE_URL", _FAKE_URL)
    monkeypatch.setattr(settings, "AI_MODEL", _FAKE_MODEL)

    with patch(_CLIENT) as client:
        client.return_value.chat.completions.create.return_value = _fake_response(_VALID_AI_JSON)
        result = AIEngine().qualify_lead(_RESEARCH_DATA, _RESEARCH_QUERY)

    # The configured endpoint and model are what actually got used.
    create = client.return_value.chat.completions.create
    assert create.called, "research AI never reached RouterProvider"
    assert create.call_args.kwargs["model"] == _FAKE_MODEL
    _, client_kwargs = client.call_args
    assert client_kwargs["base_url"] == _FAKE_URL

    # No alternative transport is even reachable any more — a stronger guarantee
    # than mocking a rival provider, because the rivals no longer exist.
    for name in _REMOVED_PROVIDER_NAMES:
        assert get_provider_class(name) is None

    # AI intelligence was blended additively into the research result.
    assert result["ai_used"] is True
    assert result["ai_score"] == 80
    assert result["qualified"] is True
