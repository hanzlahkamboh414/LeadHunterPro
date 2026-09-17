"""Access Prober — the deterministic path walker (coverage_engine_v2.md §4).

Pure Python, NO AI. For one seed row the prober walks the 5 admitted
paths in priority order (the seeded ``known_access`` hint first) and
classifies one HTTP probe per path:

    403/429/captcha → blocked (reason) → try the NEXT path
    404/410         → dead — the ONLY permanent skip (§7)
    200 + data      → access_path recorded; source stays probing

The prober owns REACHABILITY, never content: what a path yields and how
to parse it is the adapter writer's (Phase 3). Production uses a real
httpx transport; tests inject MockTransport — the fake-HTTP matrix pins
the classification, no network ever (the catalog.py pattern).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import httpx

from app.source_scout.store import (
    PROBER_PATHS,
    STATUS_BLOCKED,
    STATUS_UNTRIED,
    ScoutStore,
)

#: Probe outcome kinds — the honest four, nothing else.
KIND_OK = "ok"
KIND_BLOCKED = "blocked"
KIND_DEAD = "dead"

#: A 200 that is actually a human-verification wall counts as blocked.
CAPTCHA_MARKERS = (
    b"captcha",
    b"cf-challenge",
    b"cloudflare",
    b"are you a human",
)

#: 30-day re-arm when EVERY path is blocked (boards update monthly).
RE_ARM_DAYS = 30

#: Probe reads at most this much of the body — enough for captcha
#: detection, nowhere near a real download (that is the fetcher's job).
_MAX_PROBE_BYTES = 64 * 1024

_TIMEOUT_S = 15.0

_UA = "LeadHunterPro scout prober/2.0 (coverage engine v2)"


@dataclass(frozen=True)
class Probe:
    """One classified HTTP probe — kind + the honest why."""

    kind: str
    status_code: int
    reason: str
    content_type: str = ""


def classify(status_code: int, body: bytes = b"") -> tuple[str, str]:
    """HTTP outcome → (kind, reason) — the §4 rules, strictly applied.

    ``dead`` is 404/410 ONLY — every other failure (including 5xx and
    transport errors) is ``blocked``: retryable, never permanent.
    """
    if status_code in (404, 410):
        return KIND_DEAD, f"HTTP {status_code}"
    if status_code in (403, 429):
        return KIND_BLOCKED, f"HTTP {status_code}"
    if status_code == 200:
        low = body[:_MAX_PROBE_BYTES].lower()
        for marker in CAPTCHA_MARKERS:
            if marker in low:
                return KIND_BLOCKED, "captcha/anti-bot page (200)"
        return KIND_OK, "HTTP 200"
    return KIND_BLOCKED, f"HTTP {status_code}"


def fetch_probe(url: str, *, transport: httpx.BaseTransport | None = None,
                timeout: float = _TIMEOUT_S) -> Probe:
    """One bounded GET; transport errors → blocked with the honest type."""
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      transport=transport) as client:
        try:
            resp = client.get(url, headers={"User-Agent": _UA})
        except httpx.RequestError as exc:
            return Probe(KIND_BLOCKED, 0, f"transport: {type(exc).__name__}")
        body = resp.content[:_MAX_PROBE_BYTES] if resp.content else b""
        kind, reason = classify(resp.status_code, body)
        return Probe(kind, resp.status_code, reason,
                     resp.headers.get("content-type", ""))


def _default_candidates(row: dict[str, Any]) -> list[tuple[str, str]] | None:
    """(path, url) walk order for a seeded row: the known_access hint
    first, then the remaining paths in §4 priority order, all against the
    seeded base_url. Returns None when the seed has no URL to probe."""
    base = (row.get("base_url") or "").strip()
    if not base:
        return None
    seed = row.get("payload", {}).get("seed") or {}
    hint = str(seed.get("known_access", ""))
    order: list[str]
    if hint in PROBER_PATHS:
        order = [hint, *(p for p in PROBER_PATHS if p != hint)]
    else:
        order = list(PROBER_PATHS)
    return [(p, base) for p in order]


def walk_paths(store: ScoutStore, source_id: str, *,
               transport: httpx.BaseTransport | None = None,
               candidates: list[tuple[str, str]] | None = None
               ) -> tuple[dict[str, Any], Probe]:
    """Walk one source's paths in order; return (row, last_probe).

    The lifecycle moves exactly per §4/§7:
        untried → probing          (the walk begins)
        403/429/…  → blocked       (reason) → next path
        404/410    → dead          (terminal — walk stops)
        all blocked → blocked with a 30-day next_retry_at (re-arm)
        any 200    → access_path recorded, stays probing
    """
    row = store.get(source_id)
    if row is None:
        raise ValueError(f"unknown source_id: {source_id!r}")
    if candidates is None:
        candidates = _default_candidates(row)
        if candidates is None:
            return store.record_gate_reason(
                source_id,
                "seed has no base_url — URL verification is a fill task"), \
                Probe(KIND_BLOCKED, 0, "no base_url in seed")
    if row["status"] == STATUS_UNTRIED:
        store.start_probing(source_id)
    elif row["status"] == STATUS_BLOCKED:
        # caller re-arms before a re-walk; the clock check is theirs
        store.re_arm(source_id)

    last: Probe = Probe(KIND_BLOCKED, 0, "no path attempted")
    for i, (path, url) in enumerate(candidates):
        probe = fetch_probe(url, transport=transport)
        last = probe
        if probe.kind == KIND_OK:
            return store.record_probe_success(source_id, path), probe
        if probe.kind == KIND_DEAD:
            return store.mark_dead(source_id, f"{path}: {probe.reason}"), probe
        # blocked — record the why, then try the next path when one remains
        if i + 1 < len(candidates):
            store.mark_blocked(source_id, f"{path}: {probe.reason}")
            store.re_arm(source_id)  # blocked → probing, next path

    # every path blocked: ONE blocked transition, stamped with the
    # 30-day re-arm (last path's blocked lands here, not twice)
    retry = datetime.datetime.now(datetime.timezone.utc) + \
        datetime.timedelta(days=RE_ARM_DAYS)
    return store.mark_blocked(
        source_id,
        f"all {len(candidates)} paths blocked — re-arm in {RE_ARM_DAYS} days",
        next_retry_at=retry.strftime("%Y-%m-%dT%H:%M:%S")), last


def probe_queue(store: ScoutStore | None = None, limit: int = 10, *,
                transport: httpx.BaseTransport | None = None
                ) -> list[dict[str, Any]]:
    """Walk the next N untried phone-seed rows, in priority_rank order.

    URL-less rows (the seed gaps) are skipped with an honest reason and
    keep their untried status — they stay visible, never silently gone.
    """
    store = store or get_store()
    out: list[dict[str, Any]] = []
    for cand in store.phone_queue(limit=limit * 3):  # over-fetch: skips
        if cand["status"] != STATUS_UNTRIED:
            continue
        row = store.get(cand["source_id"])
        if row is None or not (row.get("base_url") or "").strip():
            # the seed gap: record the honest why, keep it visible, walk
            # nobody — it is not a probe outcome, so it never enters `out`
            store.record_gate_reason(
                cand["source_id"],
                "seed has no base_url — URL verification is a fill task")
            continue
        row, probe = walk_paths(store, cand["source_id"], transport=transport)
        out.append({
            "source_id": cand["source_id"],
            "state": cand["state"],
            "status": row["status"],
            "probe": probe.kind,
            "reason": row.get("gate_fail_reason") or probe.reason,
        })
        if len(out) >= limit:
            break
    return out