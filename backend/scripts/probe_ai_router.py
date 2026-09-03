"""Generic diagnostic prober for an OpenAI/Anthropic-compatible AI router.

This is a STANDALONE diagnostic — it imports no LeadHunterPro modules and uses
only the Python standard library, so it can answer "is the transport alive?"
even when the application layer is misconfigured.

It is deliberately provider-agnostic (CLAUDE.md sections 4 and 8: providers are
replaceable infrastructure). Point it at OmniRoute, NaraRouter, or any future
router; nothing about it is specific to one vendor.

What it answers, in order:

  1. Is the base URL reachable at all (vs. connection refused / wrong port)?
  2. Which auth style does it accept (none / Bearer / x-api-key)?
  3. Which model IDs does it actually serve  <- settles exact model spelling.
  4. Which wire protocol does it speak (OpenAI chat vs. Anthropic messages)?

Usage (from ``backend/``):

    # Stage A - discover endpoint, auth style and real model names (no tokens spent)
    python scripts/probe_ai_router.py

    # Stage A against a different router
    python scripts/probe_ai_router.py --base-url https://router.bynara.id/v1

    # Stage B - sweep candidate models x auth styles x protocols, stopping at the
    # first HTTP 200 so no quota is wasted once a working combination is found
    python scripts/probe_ai_router.py --skip-listing \
        --models oc/deepseek-v4-flash-free,Combo-LeadHunter,auto/best-free

Credentials are read from the gitignored ``backend/.env`` only. Every token is
scrubbed from all output, so this script is safe to paste into a chat log.

Exit code 0 when the requested stage succeeded, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://localhost:20128"
ENV_PATH = pathlib.Path(__file__).resolve().parents[1] / ".env"

# Env vars that may hold a router credential, in preference order. Every one of
# them is fed to the scrubber, so a key for router A can never leak while
# probing router B. ``AI_API_KEY`` is the application's own single credential
# variable; the rest are kept only so an older .env still probes correctly.
TOKEN_VARS = (
    "AI_API_KEY",
    "OMNIROUTE_API_KEY",
    "NARA_API_KEY",
    "OPENAI_API_KEY",
)

# The application's configured endpoint. When present it becomes the default
# probe target, so the prober follows the same single source of truth the app
# does instead of drifting from it.
BASE_URL_VAR = "AI_BASE_URL"

# Endpoint paths tried for a model listing, relative to the base URL's host.
LISTING_PATHS = ("/v1/models", "/models")

TIMEOUT_SECONDS = 20


def load_env(path: pathlib.Path) -> dict[str, str]:
    """Parse a dotenv file into a plain dict.

    Args:
        path: Path to the ``.env`` file.

    Returns:
        Mapping of key to value; empty when the file is absent.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Scrubber:
    """Redact known secrets from any text before it is printed."""

    def __init__(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def __call__(self, text: str) -> str:
        """Replace every known secret with a placeholder.

        Args:
            text: Text that may embed a credential.

        Returns:
            The text with credentials replaced.
        """
        for secret in self._secrets:
            text = text.replace(secret, "***REDACTED***")
        return text


def auth_styles(token: str, pinned: str = "auto") -> list[tuple[str, dict[str, str]]]:
    """Build the candidate auth header sets to try.

    Args:
        token: Credential value (may be empty).
        pinned: Restrict to one style once Stage A has identified it; ``"auto"``
            tries every candidate. Pinning matters for Stage B, where each
            extra style costs a real POST against the router.

    Returns:
        List of ``(label, headers)`` pairs, cheapest first.
    """
    styles: list[tuple[str, dict[str, str]]] = [("no-auth", {})]
    if token:
        styles.append(("bearer", {"Authorization": f"Bearer {token}"}))
        styles.append(("x-api-key", {"x-api-key": token, "anthropic-version": "2023-06-01"}))
    if pinned != "auto":
        styles = [(label, headers) for label, headers in styles if label == pinned]
    return styles


def request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int | None, str]:
    """Perform one HTTP request without raising.

    Args:
        url: Absolute URL.
        headers: Request headers.
        payload: JSON body; ``None`` issues a GET.

    Returns:
        ``(status_code, body)``; status is ``None`` on transport failure and the
        body then carries the exception description.
    """
    data = None
    all_headers = dict(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        all_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=all_headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - diagnostic must never crash
        return None, f"{type(exc).__name__}: {exc}"


def extract_model_ids(body: str) -> list[str]:
    """Pull model identifiers out of a listing response.

    Args:
        body: Raw JSON response body.

    Returns:
        Model IDs, or an empty list when the shape is unrecognised.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return []
    items = parsed.get("data") or parsed.get("models") or parsed if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name")
        else:
            value = str(item)
        if value:
            ids.append(str(value))
    return ids


def origin_of(base_url: str) -> str:
    """Strip a trailing API version segment so paths can be composed.

    Args:
        base_url: Base URL that may end in ``/v1``.

    Returns:
        The scheme+host origin without a trailing slash.
    """
    trimmed = base_url.rstrip("/")
    return trimmed.removesuffix("/v1")


def banner(title: str) -> None:
    """Print a section header.

    Args:
        title: Header text.
    """
    print("\n" + "=" * 68)
    print(f" {title}")
    print("=" * 68)


def stage_a(
    origin: str,
    token: str,
    scrub: Scrubber,
    needle: str,
    pinned: str = "auto",
    quiet: bool = False,
) -> tuple[bool, str | None]:
    """Discover reachability, auth style and served model IDs.

    Args:
        origin: Router origin (no version suffix).
        token: Credential value.
        scrub: Secret redactor.
        needle: Case-insensitive substring to highlight in the model list.
        pinned: Auth style to restrict to, or ``"auto"``.
        quiet: Print only the matching models, not all of them. Useful once the
            full catalogue is known and the output has to stay pasteable.

    Returns:
        ``(succeeded, working_auth_label)``.
    """
    banner("STAGE A - endpoint, auth style, real model names")
    for label, headers in auth_styles(token, pinned):
        for path in LISTING_PATHS:
            url = f"{origin}{path}"
            status, body = request(url, headers)
            print(f"[{label:>9}] GET {path:<12} -> {status}")
            if status != 200:
                print(f"{'':12}{scrub(body[:200]).replace(chr(10), ' ')}")
                continue
            ids = extract_model_ids(body)
            if not ids:
                print(f"{'':12}200 but unrecognised shape: {scrub(body[:200])}")
                continue
            print(f"\n  AUTH STYLE THAT WORKS : {label}")
            print(f"  MODELS SERVED         : {len(ids)}")
            # Group by prefix. When every model of one family is refused upstream,
            # the only way forward is a different family — which requires knowing
            # which families exist and how each one is spelled exactly.
            families: dict[str, list[str]] = {}
            for model_id in ids:
                prefix = model_id.split("/", 1)[0] if "/" in model_id else "(no prefix)"
                families.setdefault(prefix, []).append(model_id)
            print("  PROVIDER FAMILIES     : (count, prefix, example exact spelling)")
            for prefix, members in sorted(families.items(), key=lambda kv: (-len(kv[1]), kv[0])):
                print(f"    {len(members):>4}  {prefix:<22} e.g. {members[0]}")
            matches = [i for i in ids if needle.lower() in i.lower()]
            print(f"  MATCHING '{needle}'   : {matches or '(none)'}")
            if quiet:
                print("  FULL MODEL LIST       : suppressed (--quiet)")
            else:
                print("  FULL MODEL LIST       :")
                for model_id in ids:
                    print(f"    - {model_id}")
            return True, label
    print("\n  No model listing answered 200.")
    print("  Either the router exposes no listing endpoint (Anthropic-only proxy),")
    print("  or it is not listening on this URL. Re-run with --model to test chat.")
    return False, None


def build_probes(model: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Build the per-protocol chat payloads for one model.

    Args:
        model: Exact model ID.

    Returns:
        List of ``(protocol_label, path, json_payload)``.
    """
    prompt = "Reply with exactly: ROUTER_OK"
    return [
        (
            "openai-chat",
            "/v1/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 32,
            },
        ),
        (
            "anthropic-messages",
            "/v1/messages",
            {
                "model": model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": prompt}],
            },
        ),
    ]


def stage_b(
    origin: str,
    token: str,
    scrub: Scrubber,
    models: list[str],
    pinned_auth: str = "auto",
    pinned_proto: str = "auto",
) -> tuple[str, str, str] | None:
    """Find the first (model, auth, protocol) combination the router accepts.

    Probes are ordered model-outer / auth-inner so that the auth question is
    settled on the first model before moving on, and the sweep **stops at the
    first HTTP 200** so no quota is spent once a working combination is known.

    Args:
        origin: Router origin (no version suffix).
        token: Credential value.
        scrub: Secret redactor.
        models: Candidate model IDs, most-preferred first.
        pinned_auth: Auth style to restrict to, or ``"auto"``.
        pinned_proto: Protocol label to restrict to, or ``"auto"``.

    Returns:
        ``(model, auth_label, protocol_label)`` on success, otherwise ``None``.
    """
    banner("STAGE B - first working (model, auth, protocol) combination")
    codes: set[str] = set()
    statuses: set[int | None] = set()
    html_bodies = False
    for model in models:
        print(f"\n  model: {model}")
        for auth_label, headers in auth_styles(token, pinned_auth):
            for proto, path, payload in build_probes(model):
                if pinned_proto != "auto" and proto != pinned_proto:
                    continue
                status, body = request(f"{origin}{path}", headers, payload)
                print(f"    [{auth_label:>9}] {proto:<19} -> {status}")
                if status == 200:
                    print(f"{'':16}RESPONSE: {scrub(body[:400]).replace(chr(10), ' ')}")
                    print(f"\n  FIRST WORKING COMBO -> model={model} "
                          f"auth={auth_label} protocol={proto}")
                    return model, auth_label, proto
                statuses.add(status)
                snippet = scrub((body or "")[:200]).replace(chr(10), " ")
                print(f"{'':16}{snippet}")
                lowered = snippet.lower()
                if "<!doctype html" in lowered or "<html" in lowered:
                    html_bodies = True
                for known in ("insufficient_quota", "model_not_found", "invalid_api_key",
                              "authentication_error", "permission_error", "rate_limit"):
                    if known in snippet:
                        codes.add(known)
    print("\n  No combination returned 200.")
    if codes:
        print(f"  Error codes seen: {sorted(codes)}")
    if len(auth_styles(token, pinned_auth)) > 1 and len(statuses) == 1:
        only = statuses.copy().pop()
        print(f"  NOTE: every auth style returned the same status ({only}), so the")
        print("        credential is not the discriminator - sending it changes nothing.")
    if html_bodies:
        # An HTML body is the decisive clue and outranks whatever code the router
        # stamped on its envelope, so it is reported before the code-based paths.
        print("  DIAGNOSIS: the upstream answered with an HTML page, not a JSON API")
        print("             error. A genuine out-of-credit reply is JSON. An HTML 403")
        print("             means the call never reached the provider's API layer - it")
        print("             was refused at the edge: bot/WAF challenge, IP block, or an")
        print("             expired login on the ROUTER'S OWN upstream account. The")
        print("             router may still label this 'insufficient_quota'; that label")
        print("             is the router's guess, not the upstream's own code.")
        print("             Root cause therefore lives in the router's configuration and")
        print("             logs, not in this project. Read the router's console output.")
    elif "insufficient_quota" in codes:
        print("  DIAGNOSIS: the router routed the call (403, not 404) but the upstream")
        print("             provider refused it. This is an account/credit problem on")
        print("             the model's upstream, not a bug in this project. Try a model")
        print("             from a different provider family.")
    return None


def main() -> int:
    """Run the requested probe stages.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description="Probe an AI router's endpoint, auth and models.")
    parser.add_argument(
        "--base-url",
        default=None,
        help="Router base URL. Defaults to AI_BASE_URL from backend/.env, so the "
             "prober targets whatever the application itself is configured to use.",
    )
    parser.add_argument(
        "--models",
        default=None,
        help="Comma-separated model IDs to sweep in Stage B, most-preferred first. "
             "The sweep stops at the first HTTP 200 so no quota is wasted.",
    )
    parser.add_argument("--model", default=None, help="Single-model alias for --models.")
    parser.add_argument("--filter", default="deepseek", help="Substring highlighted in Stage A.")
    parser.add_argument(
        "--auth",
        default="auto",
        choices=("auto", "no-auth", "bearer", "x-api-key"),
        help="Pin the auth style once Stage A has identified it (saves real POSTs in Stage B).",
    )
    parser.add_argument(
        "--protocol",
        default="auto",
        choices=("auto", "openai-chat", "anthropic-messages"),
        help="Pin the wire protocol once it is known, to halve the request count.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the full model list in Stage A; print only --filter matches.",
    )
    parser.add_argument(
        "--skip-listing",
        action="store_true",
        help="Skip Stage A entirely when the catalogue is already known.",
    )
    parser.add_argument(
        "--api-key-var",
        default=None,
        choices=TOKEN_VARS,
        help="Which .env variable holds the credential to send. Defaults to the "
             "first non-empty one in TOKEN_VARS order. Set it explicitly when the "
             "file holds keys for several routers.",
    )
    args = parser.parse_args()

    raw_models = args.models or args.model or ""
    models = [m.strip() for m in raw_models.split(",") if m.strip()]

    env = load_env(ENV_PATH)
    token = ""
    token_var = "(none found)"
    candidates = (args.api_key_var,) if args.api_key_var else TOKEN_VARS
    for name in candidates:
        if env.get(name):
            token, token_var = env[name], name
            break
    scrub = Scrubber([env[name] for name in TOKEN_VARS if env.get(name)])

    base_url = args.base_url or env.get(BASE_URL_VAR) or DEFAULT_BASE_URL
    origin = origin_of(base_url)
    banner("AI ROUTER PROBE")
    print(f"  base url     : {base_url}")
    print(f"  origin used  : {origin}")
    print(f"  env file     : {ENV_PATH}  (exists: {ENV_PATH.is_file()})")
    print(f"  credential   : {token_var}  (length {len(token)}, value never printed)")
    print(f"  auth style   : {args.auth}")
    print(f"  protocol     : {args.protocol}")
    print(f"  models       : {models or '(Stage B skipped)'}")

    listed = True
    if args.skip_listing:
        print("\n  Stage A skipped (--skip-listing).")
    else:
        listed, _ = stage_a(origin, token, scrub, args.filter, args.auth, args.quiet)
    winner = stage_b(origin, token, scrub, models, args.auth, args.protocol) if models else None

    banner("VERDICT")
    if not args.skip_listing:
        print(f"  model listing : {'OK' if listed else 'FAILED'}")
    if models:
        if winner:
            model, auth_label, proto = winner
            print("  chat probe    : OK")
            print(f"  USE THIS      : model={model}  auth={auth_label}  protocol={proto}")
        else:
            print("  chat probe    : FAILED (no combination accepted)")
    else:
        print("  chat probe    : skipped (pass --models <id,id> to run Stage B)")
    return 0 if (winner or (listed and not models)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
