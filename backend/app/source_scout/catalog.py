"""Socrata catalog pre-fetch — real candidates so the AI never invents.

Root cause of the 2026-09-14 test-server failures: the playbook prompt
asked the LLM to NAME Socrata datasets (id + endpoint + columns), and a
flash model guesses — ``or_liuna``'s endpoint was not a SODA URL,
``co_cda``'s dataset id ``4xk7-ygij`` was a 404 hallucination. The
verifier caught both honestly, but every guessed proposal is a wasted
verify cycle, and the founder directive is that the scout must LEARN,
not guess.

This module grounds generation in reality. The public Socrata catalog
(``api.us.socrata.com/api/catalog/v1`` — the same API a portal's own
search box uses, free, no key) is queried for contractor-license
datasets, and only REAL datasets (real id, real domain, real column
names) are handed to the LLM, whose job shrinks to SELECT + MAP.
The endpoint is built here from the catalog row — the AI never writes
a URL again.

Transport discipline: every query failure is collected into the
returned ``errors`` list (a dead catalog is an honest zero for the
whole generation pass, never a silent skip and never a fallback to
letting the AI invent datasets).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

from app.harvester.store import US_STATE_ABBRS

logger = logging.getLogger(__name__)

#: The public Socrata Discovery catalog — free, keyless, the same API a
#: portal's own search page uses.
CATALOG_URL = "https://api.us.socrata.com/api/catalog/v1"

#: Search terms spread across the license-board vocabulary. Four narrow
#: terms beat one broad one — each returns up to QUERY_LIMIT real rows.
SEARCH_TERMS: tuple[str, ...] = (
    "contractor license",
    "licensed contractors",
    "electrical license",
    "plumbing license",
)

#: Results per catalog query (the API caps at 100).
QUERY_LIMIT = 100

#: How many real candidates maximum reach the prompt — the AI picks from
#: a digestible list, not a firehose.
MAX_CANDIDATES = 20

#: A candidate must expose at least one column that looks like a phone
#: number — the phones vertical's whole point (thin pre-filter; the AI
#: and the verifier judge the rest).
_PHONE_HINTS = ("phone", "telephone")

#: Sample size for real trade-column values (one request per selected
#: dataset). SODA caps $limit at 1000 for anonymous requests; 500 is
#: comfortably inside and plenty to see the value vocabulary.
SAMPLE_LIMIT = 500

_US_ABBRS = frozenset(US_STATE_ABBRS.values())


def domain_state(domain: str) -> str | None:
    """``data.oregon.gov`` -> ``OR``; ``None`` when no US state is
    attributable (city portals like ``data.cityofchicago.org`` have no
    state part and are skipped — the sanitizer requires a state code).

    Matches a dot-separated part against the full state name
    (``oregon``) or the 2-letter abbreviation (``ny``). Exact part
    equality only — ``oregonexplorer`` is not Oregon.
    """
    host = (domain or "").lower().strip()
    for part in host.split("."):
        if part in US_STATE_ABBRS:
            return US_STATE_ABBRS[part]
        if len(part) == 2 and part.upper() in _US_ABBRS:
            return part.upper()
    return None


def _candidate(hit: dict[str, Any]) -> dict[str, Any] | None:
    """One catalog row -> a candidate dict, or None if unusable."""
    resource = hit.get("resource") or {}
    domain = str((hit.get("metadata") or {}).get("domain") or "").lower()
    state = domain_state(domain)
    dataset_id = str(resource.get("id") or "").strip()
    if not state or not dataset_id:
        return None
    fields = [f for f in (resource.get("columns_field_name") or [])
              if isinstance(f, str) and f.strip()]
    labels = [str(l) for l in (resource.get("columns_name") or [])]
    if not fields:
        return None
    return {
        "id": dataset_id,
        "domain": domain,
        "state": state,
        "name": str(resource.get("name") or "")[:200],
        "description": str(resource.get("description") or "")[:300],
        "columns": fields[:80],
        "column_labels": dict(zip(fields, labels, strict=False)),
    }


def fetch_catalog_candidates(
    gap_states: set[str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Real contractor-license datasets for the uncovered states.

    Returns ``{"candidates": [...], "queries": [{"q", "results",
    "kept"}], "errors": [...]}`` — every query's outcome is visible,
    and a candidate appears only when its dataset id, domain, state and
    columns all came from a live catalog row.

    ``transport`` is httpx's mock hook — tests never touch the network.
    """
    by_id: dict[str, dict[str, Any]] = {}
    queries: list[dict[str, Any]] = []
    errors: list[str] = []
    with httpx.Client(timeout=20.0, transport=transport) as client:
        for term in SEARCH_TERMS:
            try:
                resp = client.get(
                    CATALOG_URL,
                    params={"q": term, "only": "dataset",
                            "limit": QUERY_LIMIT},
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001 — honest per-query error
                errors.append(f"q={term!r}: {type(exc).__name__}: {exc}")
                continue
            results = payload.get("results") or []
            kept = 0
            for hit in results:
                cand = _candidate(hit)
                if cand is None:
                    continue
                if cand["state"] not in gap_states:
                    continue
                if not any(h in c for c in cand["columns"]
                           for h in _PHONE_HINTS):
                    continue
                if cand["id"] in by_id:
                    continue
                by_id[cand["id"]] = cand
                kept += 1
            queries.append({"q": term, "results": len(results), "kept": kept})
    candidates = sorted(by_id.values(), key=lambda c: (c["state"], c["id"]))
    return {
        "candidates": candidates[:MAX_CANDIDATES],
        "queries": queries,
        "errors": errors,
    }


def build_endpoint(candidate: dict[str, Any]) -> str:
    """The SODA resource URL — built from the catalog row, never from
    the LLM."""
    return f"https://{candidate['domain']}/resource/{candidate['id']}.json"


def sample_column_values(
    endpoint: str,
    column: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """The REAL distinct values of one column — what the AI maps onto
    the canonical trade slugs, so it never guesses a value either.

    Returns ``{"values": [...], "rows": n}`` on success and
    ``{"values": [], "rows": 0, "error": "..."}`` on failure — an empty
    sample is an honest rejection upstream, never a silent pass.
    """
    try:
        with httpx.Client(timeout=20.0, transport=transport) as client:
            resp = client.get(
                endpoint,
                params={"$select": quote(column), "$limit": SAMPLE_LIMIT},
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:  # noqa: BLE001 — honest error, caller rejects
        return {"values": [], "rows": 0,
                "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(rows, list):
        return {"values": [], "rows": 0, "error": "non-list SODA response"}
    values = sorted({
        str(row.get(column)).strip()
        for row in rows
        if isinstance(row, dict)
        and row.get(column) is not None
        and str(row.get(column)).strip()
    })
    return {"values": values, "rows": len(rows)}
