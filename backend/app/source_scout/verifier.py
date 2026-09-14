"""Mechanical source verifier — the first trust stage (P9 sub-inc 3).

A quarantined proposal is worth exactly nothing until real numbers prove
it (:mod:`app.source_scout.propposals` style honesty — §12: a proposal is
not evidence). This module asks the source itself, with NO AI anywhere in
the loop, and every answer comes from a measured HTTP response:

  1. ERROR CLASSIFY — the sample fetch goes through the shared
     :func:`app.discovery.sources._http.fetch`, so DNS/timeout becomes
     UNAVAILABLE and HTTP errors become ERROR, exactly like every other
     source (CLAUDE.md §4: one degradation vocabulary).
  2. DNS FALLBACK — a UNAVAILABLE sample gets ONE immediate retry
     (transient resolver flakiness, the data.texas.gov lesson).
  3. IP PIN — if the retry also fails and the payload carries a
     ``pinned_ip`` (added by a human or a later learning step), one
     last attempt talks to the IP directly with the real Host header
     (unverified TLS, public read-only data — the soda.py pattern).
  4. ALT ENDPOINT — an HTTP-level failure is retried once against the
     portal's metadata endpoint (``/api/views/<id>.json``): a resource
     404 on the main URL sometimes still answers metadata, and the
     metadata is needed for recency anyway.
  5. SHAPE — the sample must be a non-empty JSON list carrying the
     proposed ``phone_column``, ``person_column``, ``trade_column`` (and
     ``status_column`` when claimed), with at least MIN_SAMPLE_ROWS rows
     that actually have a phone number.
  6. TRADE EVIDENCE — a filtered sample (``$where=trade_column IN(...)``
     of the FIRST proposed slug's values) must return rows: this proves
     the AI's value mapping, which is the part it most plausibly
     hallucinated.
  7. VOLUME — ``$select=count(1)`` must report >= MIN_SOURCE_ROWS rows
     (a state license board is thousands; a toy dataset is not coverage).
     A portal that refuses the count query falls back to the sample
     check, noted honestly in the detail.
  8. RECENCY — the metadata endpoint's ``rowsUpdatedAt`` must be within
     MAX_STALENESS_DAYS. A missing/unparsable timestamp is a NOTE, not a
     fail (the sample already proved the data serves).

Every check lands in the verdict detail; the whole pass/fail is recorded
via :meth:`ScoutStore.record_verdict` (stage ``mechanical``), and a PASS
advances the lifecycle (``mark_verified``). A FAIL leaves the proposal
in ``proposed`` — retry policy (and retirement after repeated fails)
belongs to the probation service (sub-inc 4), not here.

Everything is injected (``fetch_fn``, ``pinned_get``) so tests stay
hermetic — the network is only touched in production.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from app.discovery.sources._http import FetchResult, fetch as _default_fetch
from app.discovery.sources.status import SourceStatus
from app.source_scout.store import (
    STAGE_MECHANICAL,
    STATUS_PROPOSED,
    STATUS_VERIFIED,
    ScoutStore,
)

logger = logging.getLogger(__name__)

#: Sample size for the shape / trade-evidence fetches. 50 rows is enough
#: to see columns and prove a trade mapping without hammering the portal.
SAMPLE_LIMIT = 50

#: Rows in the unfiltered sample that must carry a non-empty phone —
#: below this the "phone source" has no phones.
MIN_SAMPLE_ROWS = 5

#: A dataset must hold at least this many rows to be coverage worth
#: promoting. A state license board is thousands of rows; a pilot/test
#: dataset is not.
MIN_SOURCE_ROWS = 200

#: The dataset must have been updated inside this window. License boards
#: publish daily; annual publishers get the benefit of the doubt up to
#: here (400 > 366 so a yearly refresh never fails purely on leap years).
MAX_STALENESS_DAYS = 400

#: One HTTP request's budget — mirrors the soda.py connector timeout.
FETCH_TIMEOUT_S = 30.0


def _metadata_url(endpoint: str) -> str:
    """https://host/resource/<id>.json → https://host/api/views/<id>.json"""
    parsed = urlparse(endpoint)
    dataset_id = parsed.path.rsplit("/", 1)[-1].removesuffix(".json")
    return f"{parsed.scheme}://{parsed.netloc}/api/views/{dataset_id}.json"


def _soql_quote(value: str) -> str:
    """Escape a SoQL string literal (single quotes doubled)."""
    return value.replace("'", "''")


def _fetch_json(
    fetch_fn: Callable[..., FetchResult],
    url: str,
    params: dict[str, Any],
) -> tuple[SourceStatus, Any, str]:
    """One fetch classified and JSON-parsed; never raises."""
    result = fetch_fn(url, params=params, timeout=FETCH_TIMEOUT_S)
    if result.status is not SourceStatus.SUCCESS:
        return result.status, None, result.error or "fetch_failed"
    try:
        return SourceStatus.SUCCESS, json.loads(result.text), ""
    except ValueError:
        return SourceStatus.ERROR, None, "bad_json"


def _pinned_get(
    url: str, params: dict[str, Any], host: str, pinned_ip: str,
) -> FetchResult:
    """The pinned-IP fallback (the soda.py pattern, generalized).

    Talks to the IP directly with the real Host header; TLS SNI is then
    the IP, so certificate verification cannot apply — only ever used
    for public read-only data after the normal fetch already failed, and
    always logged (never silent, CLAUDE.md §6). Injectable/monkeypatched
    in tests; production uses ``requests``.
    """
    import requests

    parsed = urlparse(url)
    pinned = f"{parsed.scheme}://{pinned_ip}{parsed.path}"
    logger.warning(
        "scout-verify: %s failed at the resolver — retrying once against "
        "pinned IP %s (unverified TLS: public read-only data)",
        host, pinned_ip,
    )
    try:
        resp = requests.get(
            pinned, params=params,
            headers={"Host": host, "Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S, verify=False,
        )
        ok = 200 <= resp.status_code < 300
        return FetchResult(
            status=SourceStatus.SUCCESS if ok else SourceStatus.ERROR,
            http_status=resp.status_code,
            text=resp.text if ok else "",
            final_url=pinned,
            error="" if ok else f"http_{resp.status_code}",
        )
    except Exception as exc:  # noqa: BLE001 — degrade, never raise
        return FetchResult(
            status=SourceStatus.UNAVAILABLE, error=f"pinned_retry: {exc}",
        )


def _fetch_rows(
    fetch_fn: Callable[..., FetchResult],
    pinned_get: Callable[..., FetchResult],
    url: str,
    params: dict[str, Any],
    pinned_ip: str = "",
) -> tuple[SourceStatus, list[dict[str, Any]] | None, str]:
    """Fetch rows with DNS retry + IP pin; (status, rows, reason)."""
    host = urlparse(url).netloc
    status, data, reason = _fetch_json(fetch_fn, url, params)
    if status is SourceStatus.UNAVAILABLE:
        # DNS fallback: ONE immediate retry (transient resolver flake).
        status, data, reason = _fetch_json(fetch_fn, url, params)
    if status is SourceStatus.UNAVAILABLE and pinned_ip:
        result = pinned_get(url, params, host, pinned_ip)
        if result.status is SourceStatus.SUCCESS:
            try:
                data = json.loads(result.text)
                status = SourceStatus.SUCCESS
                reason = "pinned_ip_fallback"
            except ValueError:
                status, data, reason = SourceStatus.ERROR, None, "bad_json"
    if status is not SourceStatus.SUCCESS:
        return status, None, reason
    if not isinstance(data, list):
        return SourceStatus.ERROR, None, "not_a_row_list"
    return SourceStatus.SUCCESS, data, reason


def verify_source(
    store: ScoutStore,
    source_id: str,
    *,
    fetch_fn: Callable[..., FetchResult] | None = None,
    pinned_get: Callable[..., FetchResult] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Run every mechanical check on one quarantined proposal.

    Returns ``{"source_id", "passed", "checks": [{name, ok, note}],
    "reason"}`` — always explainable. The verdict is recorded in the
    store (stage ``mechanical``) and a pass advances the proposal to
    ``verified``; a fail leaves it retryable in ``proposed``.
    """
    fetch_fn = fetch_fn or _default_fetch
    pinned_get = pinned_get or _pinned_get
    clock = clock or time.time

    row = store.get(source_id)
    if not row:
        raise ValueError(f"unknown source_id: {source_id!r}")
    if row["status"] not in (STATUS_PROPOSED,):
        return {
            "source_id": source_id, "passed": False, "checks": [],
            "reason": (
                f"status is {row['status']!r}, not {STATUS_PROPOSED!r} — "
                "nothing to verify"
            ),
        }

    payload = row["payload"]
    endpoint = row["endpoint"]
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        checks.append({"name": name, "ok": ok, "note": note})
        if not ok:
            logger.info("scout-verify %s: FAIL %s — %s",
                        source_id, name, note)

    # -- 1-3. sample fetch (error classify + DNS retry + IP pin) ----------
    status, rows, fetch_reason = _fetch_rows(
        fetch_fn, pinned_get, endpoint,
        {"$limit": str(SAMPLE_LIMIT)},
        pinned_ip=str(payload.get("pinned_ip", "")),
    )
    if status is not SourceStatus.SUCCESS and rows is None:
        # 4. alt endpoint — the metadata view sometimes serves what the
        # resource URL refuses; shape checks then run on its row samples
        # are NOT possible, so this can only rescue connectivity, and the
        # shape/trade checks below will fail honestly on an empty sample.
        alt_status, _, _ = _fetch_json(
            fetch_fn, _metadata_url(endpoint), {})
        check(
            "sample_fetch", False,
            f"{fetch_reason}; alt endpoint "
            f"{'answers' if alt_status is SourceStatus.SUCCESS else 'also failing'}",
        )
    else:
        check("sample_fetch", True, fetch_reason)

    # -- 5. shape -----------------------------------------------------------
    usable_rows = [r for r in (rows or [])
                   if isinstance(r, dict)
                   and str(r.get(payload["phone_column"], "") or "").strip()]
    if status is SourceStatus.SUCCESS and rows is not None:
        check("sample_nonempty", bool(rows), "empty dataset")
        keys: set[str] = set()
        for r in rows:
            if isinstance(r, dict):
                keys |= set(r.keys())
        for col in ("phone_column", "person_column", "trade_column"):
            expected = payload.get(col, "")
            check(f"column:{col}", expected in keys,
                  f"{expected!r} not in row keys" if expected not in keys
                  else "")
        if payload.get("status_column"):
            check("column:status_column", payload["status_column"] in keys,
                  f"{payload['status_column']!r} not in row keys"
                  if payload["status_column"] not in keys else "")
        check("sample_with_phone", len(usable_rows) >= MIN_SAMPLE_ROWS,
              f"{len(usable_rows)} rows with a phone "
              f"(< {MIN_SAMPLE_ROWS})")
    else:
        check("sample_nonempty", False, "no sample to inspect")

    # -- 6. trade evidence (the AI's value mapping on trial) ----------------
    first_slug = next(iter(payload["trade_values"]))
    values = ", ".join(
        f"'{_soql_quote(v)}'"
        for v in payload["trade_values"][first_slug]
    )
    t_status, t_rows, t_reason = _fetch_rows(
        fetch_fn, pinned_get, endpoint,
        {"$where": f"{payload['trade_column']} IN({values})",
         "$limit": str(SAMPLE_LIMIT)},
        pinned_ip=str(payload.get("pinned_ip", "")),
    )
    if t_status is SourceStatus.SUCCESS and t_rows:
        check("trade_evidence", True,
              f"{len(t_rows)} rows for slug {first_slug!r}")
    else:
        check("trade_evidence", False,
              f"no rows for {first_slug!r} values "
              f"{payload['trade_values'][first_slug]} ({t_reason})")

    # -- 7. volume -----------------------------------------------------------
    v_status, v_data, v_reason = _fetch_json(
        fetch_fn, endpoint, {"$select": "count(1)"})
    count: int | None = None
    if v_status is SourceStatus.SUCCESS and isinstance(v_data, list) \
            and v_data and isinstance(v_data[0], dict):
        for v in v_data[0].values():
            try:
                count = int(v)
                break
            except (TypeError, ValueError):
                continue
    if count is not None:
        check("volume", count >= MIN_SOURCE_ROWS,
              f"{count} rows (< {MIN_SOURCE_ROWS})" if count < MIN_SOURCE_ROWS
              else f"{count} rows")
    else:
        # The portal refused the count query — the non-empty sample is the
        # honest fallback, and the detail says exactly that.
        check("volume", bool(rows), f"count unavailable ({v_reason}); "
                                    "sample rows exist")

    # -- 8. recency (metadata; a note-level check) ----------------------------
    m_status, m_data, m_reason = _fetch_json(
        fetch_fn, _metadata_url(endpoint), {})
    updated: int | None = None
    if m_status is SourceStatus.SUCCESS and isinstance(m_data, dict):
        raw = m_data.get("rowsUpdatedAt")
        try:
            updated = int(raw)
        except (TypeError, ValueError):
            updated = None
    if updated:
        age_days = (clock() - updated) / 86400.0
        check("recency", age_days <= MAX_STALENESS_DAYS,
              f"updated {age_days:.0f} days ago"
              if age_days > MAX_STALENESS_DAYS else
              f"updated {age_days:.0f} days ago")
    else:
        check("recency", True, f"rowsUpdatedAt unavailable ({m_reason}) — noted")

    passed = all(c["ok"] for c in checks)
    detail = "; ".join(
        f"{c['name']}={'ok' if c['ok'] else 'FAIL'}"
        + (f"({c['note']})" if c["note"] else "")
        for c in checks
    )
    store.record_verdict(
        source_id, STAGE_MECHANICAL, passed, detail)
    if passed:
        store.mark_verified(source_id)

    logger.info("scout-verify %s: %s — %s", source_id,
                "PASS" if passed else "FAIL", detail[:300])
    return {
        "source_id": source_id, "passed": passed, "checks": checks,
        "reason": "" if passed else detail,
    }
