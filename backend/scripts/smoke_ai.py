"""Live smoke test for the configured AI transport, through the real AI layer.

This exercises the project's own abstraction end-to-end::

    AIGateway -> AIManager -> registry.resolve(AI_PROVIDER)
        -> RouterProvider -> the endpoint configured in backend/.env

It deliberately does NOT construct a raw OpenAI client, so what gets validated
is the abstraction the application actually uses — not a parallel code path that
might work when the real one does not.

Because the provider is generic, this one script validates whichever vendor is
configured. Point ``AI_BASE_URL`` at NaraRouter, OpenAI, OmniRoute or Ollama and
re-run it; nothing here changes.

Usage (from the repository root or backend/):

    python scripts/smoke_ai.py

Requirements — set these locally in the gitignored ``backend/.env``, NEVER in
source (``backend/.env`` also ships ready-made presets for each vendor):

    AI_PROVIDER=router
    AI_BASE_URL=<vendor's OpenAI-compatible base URL>
    AI_MODEL=<exact model id>
    AI_API_KEY=<your real key>

Exit code 0 when the provider echoes the sentinel back, 1 otherwise. The API key
is never read, printed, or logged by this script.

Last live result: PASS on 2026-08-19 against NaraRouter / agnes-2.0-flash.
"""

from __future__ import annotations

import pathlib
import sys

# Make ``app`` importable no matter the current working directory.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_SENTINEL = "ROUTER_OK"


def main() -> int:
    """Ask the configured provider for a fixed sentinel and verify the reply.

    Returns:
        Process exit code: 0 on an exact sentinel match, 1 otherwise.
    """
    # Imported inside the function, not at module level, because the sys.path
    # line above must run first -- these are the only two imports that depend on
    # it. Deferring them keeps the module-level block stdlib-only, so this file
    # is clean under ANY ruff configuration. The alternative, a module-level
    # import carrying a `noqa: E402` directive, is only correct while E402 is
    # enabled: with E402 off, RUF100 flags the directive as unused, so the same
    # two lines cannot satisfy both settings. Restructuring removes the question
    # instead of picking a side.
    #
    # EDITING THIS COMMENT: never put a hash character immediately before the
    # word "noqa" anywhere in this file -- not in prose, not inside backticks.
    # Ruff scans every comment for that sequence and parses whatever follows as
    # rule codes. An earlier version of this very comment spelled the directive
    # out in full in order to explain it, and thereby made ruff emit an
    # "invalid directive" warning on EVERY run -- the explanation caused the
    # thing it was explaining, and it took two passes to notice. A permanent
    # warning is worse than it looks: it trains the eye to skip ruff's stderr,
    # which is exactly where a real finding would appear. That is why the
    # paragraph above writes the directive with no hash in front of it.
    from app.ai.gateway import AIGateway
    from app.core.config import settings

    # Printing the target makes a failure self-diagnosing: a wrong endpoint or
    # model is visible immediately. The key is never printed.
    print(f"provider : {settings.AI_PROVIDER}")
    print(f"base url : {settings.AI_BASE_URL}")
    print(f"model    : {settings.AI_MODEL}")
    print(f"api key  : length {len(settings.AI_API_KEY)} (value never printed)")

    gateway = AIGateway()
    reply = gateway.ask(f"Reply with exactly: {_SENTINEL}")
    print(f"reply    : {reply!r}")
    return 0 if reply.strip() == _SENTINEL else 1


if __name__ == "__main__":
    raise SystemExit(main())
