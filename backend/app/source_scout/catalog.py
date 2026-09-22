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
#: number — the phones vertical's whole point.
_PHONE_HINTS = ("phone", "telephone")

#: The SELECT qualify bar's second requirement: a PERSON column
#: (owner / principal / licensee) the AI can name. Company columns —
#: ``businessname``, ``dba_name``, ``orgname``, ``company``,
#: ``facility_name``, ``employer_name`` — contain none of these
#: substrings, so a firm-name column can never satisfy this gate.
_PERSON_HINTS = (
    "principal", "owner", "officer", "holder", "firstname", "lastname",
    "surname", "contact", "applicant", "licenseename", "qualifier",
    "qualifying", "responsible", "agent", "individual",
)

#: Never a person name, however the label otherwise reads: contact
#: details are not names. The phones lane already fights junk emails
#: landing in name fields. ``licensee`` is deliberately NOT a hint in
#: its bare form — it is a substring of ``licenseexpires``.
_NOT_PERSON_HINTS = ("email", "mail", "web", "url", "phone", "telephone",
                     "fax")

#: The qualify bar's third requirement: a trade / license-type column
#: the AI can map onto the canonical slugs, and the verifier can then
#: prove with a filtered ``$where`` sample.
_TRADE_HINTS = (
    "licensetype", "licensesubtype", "licenseclass", "licensecategory",
    "specialty", "trade", "certtype", "certificatecategory",
    "classification", "endorsement",
)

#: Not a trade type however the label reads: a DBA ("trade name") is a
#: business alias, and "publicly traded" is a flag — the live NY
#: Contractor Registry carries both and must not pass on them.
_NOT_TRADE_HINTS = ("traded", "trading", "tradename")

#: Sample size for real trade-column values (one request per selected
#: dataset). SODA caps $limit at 1000 for anonymous requests; 500 is
#: comfortably inside and plenty to see the value vocabulary.
SAMPLE_LIMIT = 500

_US_ABBRS = frozenset(US_STATE_ABBRS.values())

#: A domain label cannot hold a space, so the multi-word state names are
#: only reachable squashed — ``data.newjersey.gov`` is New Jersey.
_STATE_NAME_LABELS = {name.replace(" ", ""): abbr
                      for name, abbr in US_STATE_ABBRS.items()}

#: Last label of a host we will treat as a US open-data portal. Foreign
#: ccTLDs collide with USPS codes (``.ca`` Canada / California, ``.co``
#: Colombia / Colorado). ``*.gov.au`` is not US ``.gov``.
_US_TLDS = frozenset({"gov", "us", "org", "com", "net", "edu"})


#: Municipal / county open-data portals: the host names a CITY, not a
#: state, so the state filter used to drop the biggest local license
#: boards outright — ``data.cityofchicago.org``, ``data.cityofnewyork.us``
#: (the single largest portal in the harvested universe), ``data.lacity.org``.
#: Only hosts actually seen in the live portal universe are listed; the
#: state is the one the city sits in, never a guess.
_MUNICIPAL_PORTAL_STATES: dict[str, str] = {
    "cityofchicago": "IL", "cookcountyil": "IL",
    "cityofnewyork": "NY",
    "lacity": "CA", "oaklandca": "CA", "marincounty": "CA", "smcgov": "CA",
    "sustainablesm": "CA", "sandiegocounty": "CA", "bayareametro": "CA",
    "coronaca": "CA",
    "austintexas": "TX", "dallasopendata": "TX",
    "seattle": "WA", "piercecountywa": "WA",
    "nola": "LA", "brla": "LA",
    "kcmo": "MO", "cincinnati-oh": "OH", "norfolk": "VA", "dumfriesva": "VA",
    "mesaaz": "AZ",
    "montgomerycountymd": "MD", "princegeorgescountymd": "MD",
    "howardcountymd": "MD", "ramseycountymn": "MN",
    "cambridgema": "MA", "framinghamma": "MA", "somervillema": "MA",
}


def domain_state(domain: str) -> str | None:
    """``data.oregon.gov`` -> ``OR``; ``data.ca.gov`` -> ``CA``.

    ``None`` when no US state is attributable — the sanitizer requires a
    state code, so an unattributable portal is skipped.

    The last label is the TLD and is never a state. A full state name
    matches any earlier label, exact equality only — ``oregonexplorer``
    is not Oregon, and the multi-word names arrive squashed because a
    label cannot hold a space. A 2-letter USPS code counts only when the
    labels after it are exactly ``gov`` or ``us`` (``data.ct.gov``,
    ``data.ny.us``). ``data.winnipeg.ca`` is Canada, not California.

    A municipal host carries no state label at all, so it falls back to
    :data:`_MUNICIPAL_PORTAL_STATES` — matched on the host's dot- and
    hyphen-separated tokens, which is what makes
    ``internal-sandiegocounty.data.socrata.com`` Californian.
    """
    host = (domain or "").lower().strip().strip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2 or parts[-1] not in _US_TLDS:
        return None
    labels = parts[:-1]
    for i, part in enumerate(labels):
        if len(part) == 2:
            if part.upper() not in _US_ABBRS:
                continue
            if parts[i + 1:] in (["gov"], ["us"]):
                return part.upper()
            continue
        if part in _STATE_NAME_LABELS:
            return _STATE_NAME_LABELS[part]
    for token in host.replace(".", "-").split("-"):
        if token in _MUNICIPAL_PORTAL_STATES:
            return _MUNICIPAL_PORTAL_STATES[token]
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


def _has_hint(columns: list[str], hints: tuple[str, ...],
              *, exclude: tuple[str, ...] = ()) -> bool:
    """True when a column field-name contains one of ``hints``.

    Underscores are ignored, so ``license_type`` and ``licensetype`` are
    one shape — portals spell both. ``exclude`` skips a column outright.
    """
    for col in columns:
        name = col.lower().replace("_", "")
        if any(x in name for x in exclude):
            continue
        if any(h in name for h in hints):
            return True
    return False


def _unusable_reason(columns: list[str]) -> str:
    """Why a REAL catalog candidate cannot satisfy the SELECT qualify
    bar, or ``""`` when it can.

    The AI may only name columns that exist, so a dataset missing one of
    the three cannot be legitimately selected — the sanitizer would
    reject an invented column. Dropping it here instead of in the
    sanitizer is what keeps ``MAX_CANDIDATES`` slots for datasets that
    can actually serve. The reason is reported per query, so a
    ``kept=0`` line always says which gate did the dropping.
    """
    if not _has_hint(columns, _PHONE_HINTS):
        return "no phone column"
    if not _has_hint(columns, _PERSON_HINTS, exclude=_NOT_PERSON_HINTS):
        return "no person column"
    if not _has_hint(columns, _TRADE_HINTS, exclude=_NOT_TRADE_HINTS):
        return "no trade/license-type column"
    return ""


def fetch_catalog_candidates(
    gap_states: set[str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Real contractor-license datasets for the uncovered states.

    A candidate is kept only when its dataset id, domain, state and
    columns all came from a live catalog row AND its columns can satisfy
    the SELECT qualify bar: a phone column, a person column, and a
    trade/license-type column (see :func:`_unusable_reason`).

    Returns ``{"candidates": [...], "queries": [{"q", "results", "kept",
    "skipped"}], "errors": [...]}`` — every query's outcome is visible,
    including WHY a real row was dropped.

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
            skipped: dict[str, int] = {}
            for hit in results:
                cand = _candidate(hit)
                if cand is None:
                    continue
                if cand["state"] not in gap_states:
                    continue
                reason = _unusable_reason(cand["columns"])
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                    continue
                if cand["id"] in by_id:
                    continue
                by_id[cand["id"]] = cand
                kept += 1
            queries.append({"q": term, "results": len(results),
                            "kept": kept, "skipped": skipped})
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
