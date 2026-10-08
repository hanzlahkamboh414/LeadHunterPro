"""Iowa's official active construction-contractor registration feed.

The Iowa Data Hub serves a ZIP containing newline-delimited JSON. Keep one
bounded six-hour snapshot in the harvester process so paging does not download
the full 18k-row file for every 250-row window.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
import zipfile
from datetime import date
from typing import Any, Callable

import requests

from app.discovery.sources.status import SourceStatus

logger = logging.getLogger(__name__)

IA_DATASET = "https://idh-be.iowa.gov/api/v1/datasets/1052/rows.json"
IA_SOURCE_ID = "ia_registration"
IA_TRADE_VALUES: dict[str, tuple[str, ...]] = {
    "gc": ("Residential Remodelers", "New Single-Family Housing Construction",
           "New Multifamily Housing Construction",
           "Commercial & Institutional Bldg Construction"),
    "electrical": ("Electrical and Wiring Installation",),
    "roofing": ("Roofing Contractors",),
    "painting": ("Painting & Wall Covering Contractors",),
    "concrete": ("Poured Concrete Foundation Contractors",),
    "flooring": ("Flooring Contractors",),
    "drywall": ("Drywall & Insulation Contractors",),
    "finishes": ("Finish Carpentry Contractors",),
}

_CACHE_SECONDS = 6 * 3600
_cache: tuple[float, list[dict[str, Any]]] | None = None
_lock = threading.Lock()


def _parse_archive(data: bytes, *, min_rows: int = 1000) -> list[dict[str, Any]]:
    if len(data) > 25_000_000:
        raise ValueError("Iowa export exceeds compressed size limit")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [m for m in archive.infolist() if m.filename.endswith(".json")]
        if len(members) != 1 or members[0].file_size > 100_000_000:
            raise ValueError("Iowa export shape or expanded size changed")
        with archive.open(members[0]) as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    required = {"registration_number", "business_name", "phone", "city",
                "state", "expire_date", "primary_activity"}
    if len(rows) < min_rows or not all(
        isinstance(row, dict) and required.issubset(row) for row in rows
    ):
        raise ValueError("Iowa export schema or row count changed")
    rows.sort(key=lambda row: (str(row["registration_number"]),
                               str(row["business_name"])))
    return rows


def _download_rows() -> list[dict[str, Any]]:
    response = requests.get(IA_DATASET, timeout=45)
    response.raise_for_status()
    return _parse_archive(response.content)


def _cached_rows() -> list[dict[str, Any]]:
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < _CACHE_SECONDS:
            return _cache[1]
        rows = _download_rows()
        _cache = (time.monotonic(), rows)
        return rows


def fetch_ia_records(
    slug: str, city: str = "", limit: int = 250, offset: int = 0,
    *, rows_fn: Callable[[], list[dict[str, Any]]] = _cached_rows,
) -> tuple[SourceStatus, list[dict[str, Any]], dict[str, Any]]:
    values = IA_TRADE_VALUES.get(slug)
    if not values:
        return SourceStatus.ERROR, [], {"source": IA_SOURCE_ID,
                                        "error": f"unsupported_trade: {slug}"}
    try:
        rows = rows_fn()
    except (requests.ConnectionError, requests.Timeout) as exc:
        return SourceStatus.UNAVAILABLE, [], {"source": IA_SOURCE_ID,
                                               "error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 — malformed government export
        logger.warning("Iowa registration export unavailable: %s", exc)
        return SourceStatus.ERROR, [], {"source": IA_SOURCE_ID,
                                        "error": str(exc)[:200]}

    today = date.today().isoformat()
    chosen = [row for row in rows
              if row.get("primary_activity") in values
              and (row.get("state") or "").upper() == "IA"
              and str(row.get("expire_date") or "") >= today
              and row.get("business_name") and row.get("phone")
              and (not city or (row.get("city") or "").upper().startswith(
                  city.strip().upper()))]
    window = chosen[max(0, offset):max(0, offset) + max(1, limit)]
    records = [{
        "phone": row["phone"],
        "person_name": " ".join(x for x in (row.get("first_name", ""),
                                             row.get("last_name", "")) if x),
        "business_name": row["business_name"],
        "trade_category": row["primary_activity"],
        "city": row.get("city", ""),
        "state": "IA",
        "source": IA_SOURCE_ID,
        "license_status": "ACTIVE",
        "source_url": IA_DATASET,
    } for row in window]
    return SourceStatus.SUCCESS, records, {
        "source": IA_SOURCE_ID, "dataset": "iowa-1052",
        "rows_fetched": len(window), "offset": offset,
    }
