"""Inc 8 Phase D1 — measure the CURRENT parser's output on the 3 live PDFs.

Re-fetches the three source PDFs (cudahywi-Dropbox, wtagc, vendorregistry),
parses each with the current ``PdfPlanHolderParser``, projects every row
through ``PlanHolderSource._to_company_record`` (the exact production shape
behind ``output/plan_holder_records.json``), and prints the before/after
metrics that Phase D1 was asked to report.

READ-ONLY. No production code changed. Emails/phones are masked in the
per-record detail; the aggregate counts are unmasked structure.

Usage, from backend/:
    python scripts/measure_phase_d1_metrics.py
"""

from __future__ import annotations

import io
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parent))

import requests  # noqa: E402

from app.discovery.pdf_plan_holder_parser import PdfPlanHolderParser  # noqa: E402
from app.discovery.sources.plan_holder_source import PlanHolderSource  # noqa: E402

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}
_TIMEOUT = (6, 40)

SOURCES = {
    "cudahywi-dropbox": (
        "https://cms9files1.revize.com/cudahywi/Document_Center/Department/"
        "Engineering/Bid%20Information/2016/2016-02%20%20Plan%20Holders%20List.pdf"
    ),
    "wtagc": "https://wtagc.org/wp-content/uploads/2021/01/Weekly-Project-Report-01-07-21.pdf",
    "vendorregistry-bid": (
        "https://vrapp.vendorregistry.com/Bids/View/DownloadAddendumFile?bidId="
        "54d76b03-3f44-4b2a-af35-4f806f795324&bidUpdateId=f855f580-5b15-4b84-"
        "b340-2b25371e815e&fileName=Plan+Holders+List+23-11-15-PS.pdf"
    ),
}

#: Garbage-company / corrupted-name signals (mirror the parser's own rules).
_GARBAGE_COMPANY = re.compile(
    r"(^f:|\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}|https?://|www\.|\.com|\.net|"
    r"\.org|@|^(name|address|phone|email|fax):|^plan holder|^dropbox$|"
    r"^[*+]?agc$|^[*+]|^[+*][a-z])",
    re.IGNORECASE,
)
#: Corrupted-name signals: URLs, email, field labels, role titles, or
#: 3+-token names glued by dashes/pipes (a wtagc directory-footer pattern).
_CORRUPT_NAME = re.compile(
    r"(https?://|www\.|@|\b(name|address|phone|email|fax):|"
    r"\b(executive director|director|president|manager|owner|secretary|"
    r"treasurer|vice\b)|(?:-\s*-|\|\s*\|))",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")


def _mask(text: str) -> str:
    if not text:
        return text

    def _me(m: re.Match[str]) -> str:
        local, _, domain = m.group(0).partition("@")
        return f"{local[:1]}***@{domain}"

    return _PHONE_RE.sub("[P]", _EMAIL_RE.sub(_me, text))


def _fetch(name: str, url: str) -> bytes | None:
    try:
        import doh_resolver  # type: ignore

        doh_resolver.install()
    except Exception:  # noqa: BLE001
        pass
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  FETCH FAILED {name}: {type(exc).__name__}: {exc}")
        return None
    if resp.content[:5] != b"%PDF-":
        print(f"  NOT a PDF {name}: HTTP {resp.status_code} {len(resp.content)} bytes")
        return None
    return resp.content


def main() -> int:
    source = PlanHolderSource()
    parser = PdfPlanHolderParser()
    records: list[dict] = []
    per_source: dict[str, int] = {}

    for name, url in SOURCES.items():
        data = _fetch(name, url)
        if data is None:
            continue
        result = parser.parse(data, source_url=url)
        n = 0
        for row in result.rows:
            rec = source._to_company_record(row)  # noqa: SLF001
            records.append(rec)
            n += 1
        per_source[name] = n
        print(f"parsed {name}: {n} rows  status={result.report.parse_status}")

    total = len(records)
    dropbox = sum(1 for r in records if (r["company_name"] or "").strip().lower() == "dropbox")
    named = sum(1 for r in records if (r["plan_holder"]["person"] or {}).get("name"))
    corrupted = sum(
        1 for r in records if _CORRUPT_NAME.search((r["plan_holder"]["person"] or {}).get("name") or "")
    )
    garbage_company = sum(
        1 for r in records if _GARBAGE_COMPANY.search(r["company_name"] or "")
    )
    person_bound = sum(
        1 for r in records if any(e["tier"] == "person_bound" for e in r["plan_holder"]["emails"])
    )
    complete = sum(
        1
        for r in records
        if (r["plan_holder"]["person"] or {}).get("name")
        and any(e["tier"] == "person_bound" for e in r["plan_holder"]["emails"])
        and r.get("source_url")
    )
    companies = {r["company_name"] for r in records if r["company_name"]}

    print("=" * 72)
    print("PHASE D1 — CURRENT PARSER METRICS (3 live PDFs, no fabrication)")
    print("=" * 72)
    print(f"  total records              : {total}   {per_source}")
    print(f"  F1 'Dropbox' company names : {dropbox}   (baseline 24)")
    print(f"  named persons              : {named}   (baseline 5)")
    print(f"  corrupted person names     : {corrupted}   (baseline 4)")
    print(f"  garbage company names      : {garbage_company}   (baseline 30+)")
    print(f"  person-bound emails        : {person_bound}   (baseline 3)")
    print(f"  complete records           : {complete}   (baseline 3)")
    print(f"  distinct companies         : {len(companies)}")

    print("\n  records with a person (masked detail):")
    for r in records:
        person = r["plan_holder"]["person"]
        if not person:
            continue
        name = person["name"]
        emails = ", ".join(_mask(e["email"]) for e in r["plan_holder"]["emails"])
        tag = "CORRUPT" if _CORRUPT_NAME.search(name) else "clean"
        print(f"      {name:<32} {emails:<40} [{tag}] company={r['company_name']}")

    print("\n  distinct companies:")
    for c in sorted(companies):
        print(f"      {c}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
