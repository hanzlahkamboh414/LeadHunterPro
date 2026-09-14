"""SODA (Socrata Open Data API) license-board connectors — the Phones
vertical's harvest sources.

Both datasets were live-verified 2026-09-13 (see lead-source-verification):

* **Washington L&I Contractor Licenses** — data.wa.gov ``m8qx-ubtq`` (daily
  since 2015). ``primaryprincipalname`` is a REAL person name (LAST, FIRST),
  ``specialtycode1desc`` is a 60+ value trade vocabulary, ``phonenumber``,
  city/state, ACTIVE status.
* **TDLR Texas Licenses** — data.texas.gov ``7358-krk7`` (daily since 2014).
  ``license_type`` is the trade (contractor classes only — cosmetology etc.
  is noise we never request), ``business_telephone``/``owner_telephone``,
  ``business_city_state_zip``. NO email — this is a PHONE source by design.

Structural facts encoded here:
  - Government license data NEVER contains email (verified on every source);
    the phones vertical serves phone+person+trade, not emails.
  - WA does not list "ELECTRICAL" in contractor specialties (electrical
    contractors license through a separate WA L&I program) — electrical in
    TX comes from TDLR instead. The TRADE_COVERAGE map is the honest record
    of which (trade, state) pairs each source can serve.
  - TDLR has no status column; activeness is derived from the mm/dd/yyyy
    license expiry date (>= today = active).
  - data.texas.gov DNS intermittently fails on some resolvers; requests
    support an optional pinned IP (--resolve equivalent).

P9 — AI Source Scout wiring: the hand connectors above stay the seed
coverage, and every PROMOTED scout source (:mod:`app.source_scout`)
extends it at runtime through :func:`effective_trade_coverage`. Hand
connectors always win per (trade, state) — a scout source fills gaps, it
never shadows a verified hand connector. A scout source's fetches feed
the production circuit breaker (3 consecutive fails auto-retire it and
it drops out of coverage on the next lookup).

Every fetch goes through the shared ``app.discovery.sources._http.fetch``
(never raises, classifies into SourceStatus) so these sources degrade
gracefully exactly like every other source (CLAUDE.md §4).
"""

from __future__ import annotations

import logging
import os
from datetime import date
from typing import Any

from app.discovery.sources._http import fetch
from app.discovery.sources.status import SourceStatus
from app.source_scout.store import (
    STATUS_PROMOTED,
    ScoutStore,
    default_db_path as _scout_db_path,
)

logger = logging.getLogger(__name__)

WA_DATASET = "https://data.wa.gov/resource/m8qx-ubtq.json"
TDLR_DATASET = "https://data.texas.gov/resource/7358-krk7.json"

#: data.texas.gov DNS is flaky on some resolvers; pinning the ELB IP
#: (the curl --resolve workaround, verified 2026-09-13) as an optional knob.
TDLR_PINNED_IP = "52.206.140.205"

#: canonical tradefold slug -> WA specialtycode1desc values (verified live;
#: ELECTRICAL is absent from WA contractor specialties — see module docstring).
WA_TRADE_VALUES: dict[str, list[str]] = {
    "gc": ["GENERAL"],
    "plumbing": ["PLUMBING"],
    "roofing": ["ROOFING"],
    "painting": ["PAINTING/WALLCOVERING"],
    "drywall": ["DRY WALL"],
    "flooring": ["Floor Covering and Counter Tops"],
    "landscaping": ["LANDSCAPING"],
    "concrete": ["CONCRETE"],
    "demolition": ["Demolition and Salvage"],
    "mechanical": [
        "Heating/Vent/Air-Conditioning and Refrig (HVAC/R)",
        "HVAC/RFRG",
    ],
}

#: canonical slug -> TDLR license_type values (verified live). Texas does NOT
#: license GC/drywall/roofing/painting at state level — city permits and the
#: web lane cover those there; this map is honest about what TDLR can serve.
TDLR_TRADE_VALUES: dict[str, list[str]] = {
    "electrical": ["Electrical Contractor"],
    "mechanical": ["A/C Contractor"],
}

#: slug -> {state: source_id} — which source can serve which (trade, state).
TRADE_COVERAGE: dict[str, dict[str, str]] = {
    slug: {**({"WA": "wa_license"} if slug in WA_TRADE_VALUES else {}),
           **({"TX": "tdlr_license"} if slug in TDLR_TRADE_VALUES else {})}
    for slug in (set(WA_TRADE_VALUES) | set(TDLR_TRADE_VALUES))
}


def _scout_store() -> ScoutStore | None:
    """The scout store, or None when its DB file doesn't exist yet.

    The existence check is the point: a machine that never ran the scout
    must not get ``source_scout.db`` created just because the phones lane
    imported this module. Construction is direct (never the ``get_store()``
    singleton — that one binds to the real output path unconditionally,
    which would create the file the existence check just guarded against).
    Tests monkeypatch this one seam to inject (or forbid) scout state.
    """
    path = _scout_db_path()
    if not os.path.exists(path):
        return None
    return ScoutStore(db_path=path)


def _promoted_scout_coverage() -> dict[str, dict[str, str]]:
    """``{slug: {state: source_id}}`` from PROMOTED scout sources.

    One indexed SQLite read per lookup (the codebase's per-call idiom), so
    a promotion or circuit-breaker retirement is visible to the very next
    coverage question — no cache window serving a source that just died.
    Any read failure degrades to ``{}`` (lead serving never breaks for a
    scout-store reason).
    """
    store = _scout_store()
    if store is None:
        return {}
    coverage: dict[str, dict[str, str]] = {}
    try:
        for source_id, payload in store.promoted_payloads().items():
            state = str(payload.get("state", "") or "").upper()
            if not state:
                continue  # a stateless proposal cannot sit on the grid
            for slug in payload.get("trade_values", {}):
                coverage.setdefault(slug, {})[state] = source_id
    except Exception as exc:  # noqa: BLE001 — degrade, never raise
        logger.warning("scout coverage read failed: %s", exc)
        return {}
    return coverage


def effective_trade_coverage() -> dict[str, dict[str, str]]:
    """Hand ``TRADE_COVERAGE`` + promoted scout sources, hand wins.

    This is the coverage the phones lane actually serves from (P9): the
    hand-verified WA/TDLR connectors keep every (trade, state) they own,
    and scout sources only fill the pairs nobody hand-wired.
    """
    merged: dict[str, dict[str, str]] = {
        slug: dict(states) for slug, states in TRADE_COVERAGE.items()
    }
    for slug, states in _promoted_scout_coverage().items():
        for state, src in states.items():
            merged.setdefault(slug, {}).setdefault(state, src)
    return merged


def covered_sources(slug: str, state: str = "") -> list[str]:
    """Source ids that can serve this trade (optionally in this state).

    ``state=''`` (any) returns every source covering the trade. An empty
    list is an honest "no phone source covers this yet", never a fake fetch.
    """
    coverage = effective_trade_coverage().get(slug, {})
    if state:
        src = coverage.get(state.upper())
        return [src] if src else []
    return sorted(coverage.values())


def state_sources(state: str) -> list[str]:
    """Source ids covering a STATE at all, across every trade (P7.5's
    trade-less search: the user picks state + quantity only, so coverage
    means "some license board stocks this state" — any trade)."""
    st = (state or "").strip().upper()
    if not st:
        return sorted({src for m in effective_trade_coverage().values()
                       for src in m.values()})
    return sorted({src for m in effective_trade_coverage().values()
                   for s, src in m.items() if s == st})


def _soql_quote(value: str) -> str:
    """Escape a SoQL string literal (single quotes doubled)."""
    return value.replace("'", "''")


def _wa_where(slug: str, city: str) -> str:
    values = ", ".join(f"'{_soql_quote(v)}'" for v in WA_TRADE_VALUES[slug])
    clauses = [
        f"specialtycode1desc IN({values})",
        "contractorlicensestatus='ACTIVE'",
    ]
    if city:
        clauses.append(f"city LIKE '{_soql_quote(city.strip().upper())}%'")
    return " AND ".join(clauses)


def _parse_wa_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "phone": row.get("phonenumber", ""),
        "person_name": row.get("primaryprincipalname", ""),
        "business_name": row.get("businessname", ""),
        "trade_category": row.get("specialtycode1desc", ""),
        "city": row.get("city", ""),
        "state": row.get("state", "WA"),
        "source": "wa_license",
        "license_status": row.get("contractorlicensestatus", ""),
        "source_url": "https://data.wa.gov/resource/m8qx-ubtq.json",
    }


def _tdlr_active(expiry_mmddyyyy: str) -> str:
    """'ACTIVE' when the license has not expired, else the honest status."""
    try:
        exp = date(
            int(expiry_mmddyyyy[6:10]),
            int(expiry_mmddyyyy[0:2]),
            int(expiry_mmddyyyy[3:5]),
        )
        return "ACTIVE" if exp >= date.today() else "EXPIRED"
    except (ValueError, IndexError):
        return ""


def _parse_tdlr_row(row: dict[str, Any]) -> dict[str, Any]:
    # "BUDA TX 78610-4477" -> city BUDA, state TX
    city_state = (row.get("business_city_state_zip", "") or "").split()
    city = city_state[0].upper() if city_state else ""
    state = city_state[1].upper() if len(city_state) > 1 else "TX"
    return {
        "phone": row.get("business_telephone", "") or row.get("owner_telephone", ""),
        "person_name": row.get("owner_name", ""),
        "business_name": row.get("business_name", ""),
        "trade_category": row.get("license_type", ""),
        "city": city,
        "state": state,
        "source": "tdlr_license",
        "license_status": _tdlr_active(
            row.get("license_expiration_date_mmddccyy", "")
        ),
        "source_url": "https://data.texas.gov/resource/7358-krk7.json",
    }


def _fetch_page(
    url: str, params: dict[str, Any], pinned_ip: str = "",
) -> tuple[SourceStatus, list[dict[str, Any]], str]:
    """One SODA GET; returns (status, rows, honest reason).

    DNS/connection failure gets ONE pinned-IP retry when an IP is known
    (the data.texas.gov resolver flakiness, verified 2026-09-13). The retry
    talks to the IP directly with the real Host header — TLS SNI is then the
    IP, so certificate verification cannot apply; it is only ever used for
    this public read-only data after a normal fetch already failed, and the
    fallback is logged (never silent, CLAUDE.md §6).
    """
    import json

    result = fetch(url, params=params, timeout=30.0)
    if result.status is not SourceStatus.UNAVAILABLE or not pinned_ip:
        if result.status == SourceStatus.SUCCESS:
            try:
                rows = json.loads(result.text)
            except ValueError:
                return SourceStatus.ERROR, [], "bad_json"
            if not isinstance(rows, list):
                return SourceStatus.ERROR, [], "unexpected_shape"
            return SourceStatus.SUCCESS, rows, ""
        return result.status, [], result.error or "fetch_failed"

    # Pinned-IP retry (DNS failed on the normal path). The host is replaced
    # generically (the --resolve equivalent for whichever portal is pinned).
    try:
        import requests

        host = url.split("/")[2]
        pinned = url.replace(host, pinned_ip)
        logger.warning(
            "SODA fetch to %s failed at the resolver — retrying once against "
            "pinned IP %s (unverified TLS: public read-only data)",
            host, pinned_ip,
        )
        resp = requests.get(
            pinned, params=params,
            headers={"Host": host, "Accept": "application/json"},
            timeout=30.0, verify=False,
        )
        if not 200 <= resp.status_code < 300:
            return SourceStatus.ERROR, [], f"http_{resp.status_code}"
        rows = json.loads(resp.text)
        if not isinstance(rows, list):
            return SourceStatus.ERROR, [], "unexpected_shape"
        return SourceStatus.SUCCESS, rows, ""
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        return SourceStatus.UNAVAILABLE, [], f"pinned_retry_failed: {exc}"


def _scout_promoted_source(source_id: str) -> dict[str, Any] | None:
    """The PROMOTED scout source row (payload parsed) or None.

    Only promoted sources are servable — everything else in the scout
    store is quarantine by construction, and asking for it here is the
    same honest "unknown source" as asking for a source that never existed.
    """
    store = _scout_store()
    if store is None:
        return None
    try:
        row = store.get(source_id)
    except Exception as exc:  # noqa: BLE001 — degrade, never raise
        logger.warning("scout lookup for %s failed: %s", source_id, exc)
        return None
    if row and row["status"] == STATUS_PROMOTED:
        return row
    return None


def _parse_scout_row(row: dict[str, Any], payload: dict[str, Any],
                     source_id: str, endpoint: str) -> dict[str, Any]:
    """Normalize one raw scout-source row via the PROPOSED column mapping
    (the same mapping the mechanical verifier proved and agnes judged —
    the parser never guesses column names on its own)."""
    status_col = payload.get("status_column", "")
    business_col = payload.get("business_column", "business_name")
    city_col = payload.get("city_column", "city")
    return {
        "phone": row.get(payload["phone_column"], ""),
        "person_name": row.get(payload["person_column"], ""),
        "business_name": row.get(business_col, ""),
        "trade_category": row.get(payload["trade_column"], ""),
        "city": row.get(city_col, ""),
        "state": payload.get("state", ""),
        "source": source_id,
        "license_status": row.get(status_col, "") if status_col else "",
        "source_url": endpoint,
    }


def _record_scout_outcome(source_id: str, ok: bool, detail: str = "") -> None:
    """Feed the scout's production circuit breaker (3 consecutive fails
    auto-retire the source). A scout-store hiccup is logged and swallowed —
    lead serving never breaks for a scout reason."""
    store = _scout_store()
    if store is None:
        return
    try:
        from app.source_scout.probation import record_production_outcome
        record_production_outcome(store, source_id, ok, detail)
    except Exception as exc:  # noqa: BLE001 — degrade, never raise
        logger.warning(
            "scout production outcome for %s not recorded: %s",
            source_id, exc,
        )


def _fetch_scout_records(
    source_id: str, slug: str, city: str, limit: int,
) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
    """Fetch + normalize one promoted scout source's rows for a trade slug.

    The $where is built from the payload's PROVEN trade_values mapping
    (verified mechanically + judged on probation) — never re-invented
    here. Every outcome feeds the circuit breaker.
    """
    row = _scout_promoted_source(source_id)
    if row is None:
        return (SourceStatus.ERROR, [],
                {"error": f"unknown_source: {source_id}"})
    payload = row["payload"]
    values = payload.get("trade_values", {}).get(slug)
    if not values:
        return (SourceStatus.ERROR, [],
                {"source": source_id,
                 "error": f"scout source does not cover trade {slug!r}"})

    quoted = ", ".join(f"'{_soql_quote(v)}'" for v in values)
    where = f"{payload['trade_column']} IN({quoted})"
    city_col = payload.get("city_column", "")
    if city and city_col:
        where += (f" AND {city_col} LIKE "
                  f"'{_soql_quote(city.strip().upper())}%'")
    params = {"$where": where, "$limit": str(limit)}
    status, rows, reason = _fetch_page(
        row["endpoint"], params,
        pinned_ip=str(payload.get("pinned_ip", "")),
    )
    meta: dict[str, Any] = {"source": source_id, "dataset": "scout"}
    if status != SourceStatus.SUCCESS:
        _record_scout_outcome(source_id, False, reason)
        meta["error"] = reason
        logger.warning("scout source %s unavailable for %r: %s",
                       source_id, slug, reason)
        return status, [], meta
    records = [
        _parse_scout_row(r, payload, source_id, row["endpoint"])
        for r in rows
    ]
    meta["rows_fetched"] = len(rows)
    _record_scout_outcome(source_id, True)
    logger.info("scout source %s: %d rows for trade slug %r city=%r",
                source_id, len(rows), slug, city)
    return status, records, meta


def fetch_license_records(
    source_id: str, slug: str, city: str = "", limit: int = 200,
) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
    """Fetch one source's records for a canonical trade slug.

    Returns (status, normalized phone-lead records, metadata). Records with
    no usable phone are still returned — the store's add() honestly drops
    and counts them (never a silent mangle).
    """
    limit = max(1, min(limit, 5000))
    if source_id == "wa_license":
        params = {"$where": _wa_where(slug, city), "$limit": str(limit)}
        status, rows, reason = _fetch_page(WA_DATASET, params)
        meta = {"source": "wa_license", "dataset": "m8qx-ubtq"}
    elif source_id == "tdlr_license":
        values = ", ".join(
            f"'{_soql_quote(v)}'" for v in TDLR_TRADE_VALUES[slug]
        )
        where = f"license_type IN({values})"
        if city:
            where += (f" AND business_city_state_zip LIKE "
                      f"'{_soql_quote(city.strip().upper())}%'")
        params = {"$where": where, "$limit": str(limit)}
        status, rows, reason = _fetch_page(TDLR_DATASET, params, TDLR_PINNED_IP)
        meta = {"source": "tdlr_license", "dataset": "7358-krk7"}
    else:
        # P9: anything else must be a PROMOTED scout source — quarantine
        # and retired sources get the same honest unknown_source refusal.
        return _fetch_scout_records(source_id, slug, city, limit)

    if status == SourceStatus.SUCCESS:
        parse = _parse_wa_row if source_id == "wa_license" else _parse_tdlr_row
        records = [parse(r) for r in rows]
        meta["rows_fetched"] = len(rows)
        logger.info(
            "SODA %s: %d rows for trade slug %r city=%r",
            source_id, len(rows), slug, city,
        )
        return status, records, meta
    meta["error"] = reason
    logger.warning("SODA %s unavailable for %r: %s", source_id, slug, reason)
    return status, [], meta
