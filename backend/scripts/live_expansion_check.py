"""Live proof that the discovery AI lane answers — the post-deploy gate.

WHY THIS EXISTS (2026-09-18): the main server's discovery was dead for days and
every surface said it was fine. `systemctl is-active` said `active`, `/health`
said 200, and SearXNG answered — but `generate_query_expansion` (the FIRST AI
call of every job) was returning HTTP 402 "Insufficient credits" 757 times in
the journal, so no job ever got past its first step. A liveness check cannot
see that; only exercising the real unit can.

So this runs the EXACT failing unit on the deployed code path
(``ai_ask=None`` -> whatever lane the shipped config builds), and exits
non-zero when the lane produces nothing. Use it after every deploy that touches
``app/core/config.py``, ``app/ai/`` or ``app/discovery/query_expansion.py``.

It spends ONE model call. Run it on the server with the service's own env:

    sudo bash -c 'set -a; . /opt/leadhunter/.env; set +a; \
        cd /opt/leadhunter/backend; \
        /opt/leadhunter/venv/bin/python scripts/live_expansion_check.py'

A PASS is "the lane answered and produced variants". A FAIL prints the honest
reason the lane gave (the same string the job event stream carries), never a
bare boolean — §6.
"""

from __future__ import annotations

import os
import sys

# `python scripts/x.py` puts **scripts/** on sys.path, not backend/ — so
# `import app` would fail without this (same fix as clean_dead_domains.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings  # noqa: E402
from app.discovery.query_expansion import generate_query_expansion  # noqa: E402

TRADE = "General Contractors"
LOCATION = "San Antonio TX"


def main() -> int:
    """Exercise the live expansion lane and report what it actually did.

    Returns:
        Process exit code: 0 when the lane produced variants, 1 otherwise.
    """
    print("=" * 68)
    print(" LIVE DISCOVERY AI LANE CHECK")
    print("=" * 68)
    # Model names are not secrets; keys are never printed.
    print(f"  main model : {settings.AI_MODEL}")
    print(f"  deep model : {settings.AI_MODEL_DEEP}")
    print(f"  provider   : {settings.AI_PROVIDER}")
    print(f"  query      : trade={TRADE!r} location={LOCATION!r}")
    print()

    result = generate_query_expansion(TRADE, LOCATION)

    trades = result.get("trade_variants") or []
    locations = result.get("location_variants") or []
    reason = result.get("reason") or ""
    replies = result.get("raw_replies") or []

    print(f"  trade_variants    : {len(trades)}")
    print(f"  location_variants : {len(locations)}")
    print(f"  llm replies       : {len(replies)}")
    print(f"  reason            : {reason or '(none)'}")
    if trades:
        print(f"  sample trades     : {trades[:4]}")
    if locations:
        print(f"  sample locations  : {locations[:4]}")
    print()

    if trades:
        print(" VERDICT: PASS — the discovery AI lane answered live")
        return 0

    # No variants is the failure mode this script exists to catch. The lane is
    # allowed to return empty WITH a reason (no key, LLM failure, unusable
    # output) — that reason is the finding, so it is printed, never swallowed.
    print(" VERDICT: FAIL — the discovery AI lane produced no expansion")
    print(f"          the lane's own reason: {reason or '(empty — no reason given)'}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
