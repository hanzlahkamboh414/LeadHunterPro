"""Forensic test: capture the EXACT wire request RouterProvider sends.

Touches no real endpoint. It builds a real ``openai.OpenAI`` client backed by an
``httpx.MockTransport`` so the SDK performs its true serialization, then inspects
the actual request body (model / messages / any SDK-injected defaults).

This exists because a router rejecting a request is ambiguous — the fault can be
the credential, the model id, or the request shape. Pinning the shape here means
that third possibility can be ruled out without spending a live call, and it
guards the plain-text contract: several routers reject multimodal content parts,
so the message content must stay a ``str``.

Run: python -m pytest tests/ai/test_router_request_shape.py -q -s
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import openai

from app.ai.providers.router_provider import RouterProvider
from app.core.config import settings

_CLIENT = "app.ai.providers.router_provider.openai.OpenAI"
_FAKE_URL = "https://router.test.invalid/v1"
_FAKE_KEY = "test-key-not-a-real-credential"
_FAKE_MODEL = "test-model-not-real"


class _Recorder:
    """Captures the outgoing request and returns a canned 200 completion."""

    def __init__(self) -> None:
        self.request: httpx.Request | None = None
        self.body: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        try:
            self.body = json.loads(request.content)
        except Exception:  # noqa: BLE001
            self.body = None
        payload = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": _FAKE_MODEL,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )


def test_capture_exact_wire_request(monkeypatch, capsys) -> None:
    """Build a RouterProvider whose OpenAI client hits a mock transport, then
    capture the exact request body the SDK serializes."""
    monkeypatch.setattr(settings, "AI_API_KEY", _FAKE_KEY)
    monkeypatch.setattr(settings, "AI_BASE_URL", _FAKE_URL)
    monkeypatch.setattr(settings, "AI_MODEL", _FAKE_MODEL)

    recorder = _Recorder()
    with patch(_CLIENT):  # __init__ stays mocked, so no real client is built there
        provider = RouterProvider()
    # Swap in a REAL openai client on a mock transport so the SDK does its real
    # serialization and the true wire body is captured.
    provider._client = openai.OpenAI(
        api_key=_FAKE_KEY,
        base_url=_FAKE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(recorder)),
    )

    out = provider.generate("Reply with exactly: ROUTER_OK")

    body = recorder.body
    assert body is not None, "request body was not valid JSON"

    # --- the forensic detail (printed only; no credential is ever printed) ---
    print("\n[forensic] top-level request keys:", sorted(body.keys()))
    print("[forensic] model:", body.get("model"))
    print("[forensic] messages:", body.get("messages"))
    for key in (
        "temperature",
        "max_tokens",
        "max_completion_tokens",
        "response_format",
        "tools",
        "tool_choice",
        "stream",
        "user",
    ):
        if key in body:
            print(f"[forensic] extra param '{key}':", body[key])

    messages = body.get("messages", [])
    assert messages, "no messages in request"
    for message in messages:
        assert message.get("role") in ("system", "user", "assistant"), message
        content = message.get("content")
        # Content must be plain text (str), never a multimodal/image part: several
        # OpenAI-compatible routers reject the multimodal form outright.
        assert isinstance(content, str), (
            f"non-text content type {type(content)!r} — many routers would reject this"
        )

    assert body["model"] == _FAKE_MODEL
    assert out == "ok"


def test_request_body_carries_no_credential() -> None:
    """The key travels in the Authorization header, never in the JSON body.

    Anything in the body could end up in a router's request log, so this pins the
    credential to the header where the SDK puts it.
    """
    recorder = _Recorder()
    with patch(_CLIENT):
        provider = RouterProvider()
    provider._client = openai.OpenAI(
        api_key=_FAKE_KEY,
        base_url=_FAKE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(recorder)),
    )
    provider._model = _FAKE_MODEL
    provider.generate("hi")

    assert recorder.request is not None
    assert _FAKE_KEY not in recorder.request.content.decode()
    assert recorder.request.headers.get("authorization") == f"Bearer {_FAKE_KEY}"
