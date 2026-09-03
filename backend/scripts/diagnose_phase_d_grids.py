"""Inc 8 Phase D DIAGNOSTIC — dump raw pdfplumber grids behind live failure patterns.

READ-ONLY. No production code changed. Fetches the three live plan-holder PDFs
and dumps, per page, every detected table so the name/email/phone CELL
RELATIONSHIPS can be inspected before Phase D is designed.

MASKING: emails and phone numbers are masked to preserve the column structure
(the diagnostic needs "is the name in a separate cell from the email?") without
splashing full personal contacts. Masking is lossless for structure.

Usage, from backend/:
    python scripts/diagnose_phase_d_grids.py
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

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*;q=0.8",
}
_TIMEOUT = (6, 40)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

#: Case -> search tokens (case-insensitive substrings across the row).
CASES = {
    "sam@napc.me (case1)": ["napc"],
    "emills@isqft.com (case2)": ["isqft"],
    "Charity Roberts / abilene@wtagc.org (case3)": ["abilene", "charity", "wichitafalls"],
    "F1 Dropbox PDF (case4)": ["dropbox"],
}

SOURCES = {
    "wtagc": "https://wtagc.org/wp-content/uploads/2021/01/Weekly-Project-Report-01-07-21.pdf",
    "cudahywi-dropbox": (
        "https://cms9files1.revize.com/cudahywi/Document_Center/Department/"
        "Engineering/Bid%20Information/2016/2016-02%20%20Plan%20Holders%20List.pdf"
    ),
    "vendorregistry-bid": (
        "https://vrapp.vendorregistry.com/Bids/View/DownloadAddendumFile?bidId="
        "54d76b03-3f44-4b2a-af35-4f806f795324&bidUpdateId=f855f580-5b15-4b84-"
        "b340-2b25371e815e&fileName=Plan+Holders+List+23-11-15-PS.pdf"
    ),
}


def _mask(text: str) -> str:
    if not text:
        return text

    def _me(m: re.Match[str]) -> str:
        local, _, domain = m.group(0).partition("@")
        return f"{local[:1]}***@{domain}"

    masked = _EMAIL_RE.sub(_me, text)
    masked = _PHONE_RE.sub("[P]", masked)
    return masked


def _fetch(name: str, url: str) -> bytes | None:
    print(f"\n===== FETCH {name} =====")
    try:
        import doh_resolver

        doh_resolver.install()
    except Exception:  # noqa: BLE001
        pass
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  FETCH FAILED: {type(exc).__name__}: {exc}")
        return None
    print(f"  HTTP {resp.status_code}  {len(resp.content):,} bytes")
    if resp.content[:5] != b"%PDF-":
        print("  NOT a PDF; skipping.")
        return None
    return resp.content


def _dump_tables(name: str, data: bytes) -> None:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        print(f"\n########## {name}: {len(pdf.pages)} pages ##########")
        for pnum, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables() or []
            # Only print pages that contain a table OR flat text with a relevant
            # token. Plan-holder names/emails often sit in free text, not a grid.
            flat = page.extract_text() or ""
            flat_hits = any(
                t in flat.lower() for t in [t for toks in CASES.values() for t in toks]
            )
            table_hits = []
            for tnum, table in enumerate(tables):
                for r in table or []:
                    joined = " ".join((c or "") for c in r).lower()
                    if any(t in joined for t in [t for toks in CASES.values() for t in toks]):
                        table_hits.append(tnum)
                        break
            if not (flat_hits or table_hits):
                continue
            print(f"\n--- {name} PAGE {pnum}: {len(tables)} table(s) ---")
            if flat_hits:
                print("  [FLAT TEXT around matches]")
                lines = flat.splitlines()
                for i, line in enumerate(lines):
                    low = line.lower()
                    if any(t in low for t in [t for toks in CASES.values() for t in toks]):
                        lo = max(0, i - 3)
                        hi = min(len(lines), i + 4)
                        for j in range(lo, hi):
                            mark = ">>>" if j == i else "   "
                            print(f"      {mark} {_mask(lines[j])}")
                        print("      ...")
            for tnum, table in enumerate(tables):
                ncols = max((len(r) for r in table), default=0)
                hit = tnum in table_hits
                tag = "  <<< TABLE MATCH" if hit else ""
                print(f"  [Table {tnum}] {len(table)} rows x ~{ncols} cols{tag}")
                for r in table:
                    cells = [(_mask(c) if c else "") for c in r]
                    print(f"    | {' | '.join(cells)}")


def _dump_all(name: str, data: bytes, max_pages: int = 3) -> None:
    """Dump every table + full flat text on the first pages (unfiltered)."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        print(f"\n########## {name}: {len(pdf.pages)} pages (UNFILTERED first {max_pages}) ##########")
        for pnum, page in enumerate(pdf.pages[:max_pages], start=1):
            print(f"\n--- {name} PAGE {pnum} — FLAT TEXT ---")
            text = page.extract_text() or ""
            for line in text.splitlines():
                print(f"      {_mask(line)}")
            tables = page.extract_tables() or []
            print(f"--- {name} PAGE {pnum} — {len(tables)} TABLE(S) ---")
            for tnum, table in enumerate(tables):
                ncols = max((len(r) for r in table), default=0)
                print(f"  [Table {tnum}] {len(table)} rows x ~{ncols} cols")
                for r in table:
                    cells = [(_mask(c) if c else "") for c in r]
                    print(f"    | {' | '.join(cells)}")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else None
    unfiltered = sys.argv[1] == "--all" or sys.argv[2] == "--all" if len(sys.argv) > 2 else False
    if "--all" in sys.argv:
        unfiltered = True
    for name, url in SOURCES.items():
        if which and which not in name and which != "--all":
            continue
        data = _fetch(name, url)
        if data is None:
            continue
        try:
            if unfiltered:
                _dump_all(name, data)
            else:
                _dump_tables(name, data)
        except Exception as exc:  # noqa: BLE001
            print(f"  DUMP FAILED: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
