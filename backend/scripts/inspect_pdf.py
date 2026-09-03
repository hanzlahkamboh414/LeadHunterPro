"""Inspect a plan-holder PDF's REAL structure before building a parser.

READ-ONLY / one-shot diagnostic (CLAUDE.md §7 look-before-build). Reuses the
existing `requests` transport and `doh_resolver` DNS repair used by the other
scripts here. Requires `pdfplumber` (the Inc 1 dependency).

WHY THIS EXISTS
    `scripts/probe_search_dork.py` proved the dorks surface real plan-holder
    PDFs (45/98 on 2026-08-21). Before writing a single line of parser, the
    parser must be designed against ONE real document's actual layout -- not a
    guess. This dumps that layout: page count, flat text, and cell-level tables.

PII SAFETY
    These are PUBLIC government/consulting-firm records, but this dump may be
    pasted into a chat log, so emails and phone numbers are MASKED in every
    printed line. Masking preserves the COLUMN STRUCTURE the parser needs
    (a name column, an email column, a phone column) without splashing full
    contacts. Company names, project titles and headers print as-is. Raw email
    and phone COUNTS are reported as plain numbers.

WHAT IT DOES NOT KNOW
    A scanned/image PDF yields little or no extractable text -- reported as a
    "likely scanned" hint, never as "no data". That case needs OCR, a later
    increment, not this parser.

Usage, from backend/:
    python scripts/inspect_pdf.py https://www.hrgreen.com/.../Plan-Holder-List_20250121.pdf
    python scripts/inspect_pdf.py <url> --save tests/fixtures/pdf/hrgreen_plan_holder_list.pdf
    python scripts/inspect_pdf.py path/to/local.pdf
"""

from __future__ import annotations

import io
import pathlib
import re
import sys

# Same two-bootstrap trick as scripts/probe_page_links.py: put backend/ on the
# path for `import app`, and this dir on the path for the sibling doh_resolver.
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
    "Accept-Language": "en-US,en;q=0.5",
}
_TIMEOUT = (6, 25)  # (connect, read) — same rationale as probe_page_links.py

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")

_TEXT_PREVIEW_CHARS = 1600
_MAX_PAGES = 3
_MAX_TABLES_PER_PAGE = 2
_MAX_ROWS_PER_TABLE = 15


def _mask(text: str) -> str:
    """Mask emails and phones, keep structure. Applied to every printed line."""
    if not text:
        return text

    def _me(m: re.Match[str]) -> str:
        addr = m.group(0)
        local, _, domain = addr.partition("@")
        return f"{local[:1]}***@{domain}"

    masked = _EMAIL_RE.sub(_me, text)
    masked = _PHONE_RE.sub("[phone]", masked)
    return masked


def _load_bytes(source: str, save_to: str | None) -> tuple[bytes, str]:
    """Return (pdf_bytes, note). Fetches a URL or reads a local path."""
    if source.startswith(("http://", "https://")):
        try:
            import doh_resolver

            doh_resolver.install()  # inert on a healthy resolver
        except Exception:  # noqa: BLE001
            pass
        resp = requests.get(
            source, headers=_HEADERS, timeout=_TIMEOUT, allow_redirects=True
        )
        note = (
            f"HTTP {resp.status_code}  {len(resp.content):,} bytes  "
            f"Content-Type={resp.headers.get('Content-Type', '-')}  "
            f"final={resp.url}"
        )
        data = resp.content
        if save_to:
            dest = pathlib.Path(save_to)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            note += f"\n  SAVED -> {dest}"
        return data, note

    path = pathlib.Path(source)
    data = path.read_bytes()
    return data, f"local file  {len(data):,} bytes  {path}"


def inspect(source: str, save_to: str | None) -> int:
    print("=" * 100)
    print(f"INSPECT PDF: {source}")
    print("=" * 100)

    try:
        data, note = _load_bytes(source, save_to)
    except Exception as exc:  # noqa: BLE001
        print(f"  LOAD FAILED: {type(exc).__name__}: {exc}")
        print("  Nothing parsed. If this is a URL, try scripts/network_diagnostics.py.")
        return 1
    print(f"  Source      : {note}")

    is_pdf = data[:5] == b"%PDF-"
    print(f"  %PDF header : {'yes' if is_pdf else 'NO (may be an HTML error page)'}")
    if not is_pdf:
        head = data[:200].decode("utf-8", "replace").replace("\n", " ")
        print(f"  First bytes : {head}")
        print("  Not a PDF -> extraction skipped. The URL likely returned a login")
        print("  wall or an HTML error; try another candidate URL.")
        return 1

    try:
        import pdfplumber
    except ImportError:
        print("\n  pdfplumber is NOT installed. It is the Inc 1 dependency:")
        print("      python -m pip install pdfplumber")
        return 2

    full_text_parts: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        print(f"  Pages       : {page_count}")

        for page in pdf.pages:
            full_text_parts.append(page.extract_text() or "")
        full_text = "\n".join(full_text_parts)

        email_count = len(_EMAIL_RE.findall(full_text))
        phone_count = len(_PHONE_RE.findall(full_text))
        print(f"  Text length : {len(full_text):,} chars across {page_count} page(s)")
        print(f"  Emails found: {email_count}   Phones found: {phone_count}")
        if len(full_text) < 100 and page_count > 0:
            print("  HINT: almost no extractable text -> LIKELY A SCANNED/IMAGE PDF")
            print("        (needs OCR, a later increment; not this parser).")

        for pnum, page in enumerate(pdf.pages[:_MAX_PAGES], start=1):
            print("\n" + "-" * 100)
            print(f"  PAGE {pnum} — FLAT TEXT (first {_TEXT_PREVIEW_CHARS} chars, masked)")
            print("-" * 100)
            text = page.extract_text() or "(no extractable text on this page)"
            print(_mask(text[:_TEXT_PREVIEW_CHARS]))

            tables = page.extract_tables() or []
            print(f"\n  PAGE {pnum} — TABLES DETECTED: {len(tables)}")
            for tnum, table in enumerate(tables[:_MAX_TABLES_PER_PAGE], start=1):
                rows = table or []
                ncols = max((len(r) for r in rows), default=0)
                print(f"    Table {tnum}: {len(rows)} rows x ~{ncols} cols")
                for r in rows[:_MAX_ROWS_PER_TABLE]:
                    cells = [(_mask(c) if c else "") for c in r]
                    print(f"      | {' | '.join(cells)}")
                if len(rows) > _MAX_ROWS_PER_TABLE:
                    print(f"      ... {len(rows) - _MAX_ROWS_PER_TABLE} more rows")

    print("\n" + "=" * 100)
    print("  Read this to design the parser: are the columns clean in TABLES, or")
    print("  only visible in FLAT TEXT? Which columns exist (company / contact /")
    print("  email / phone / project)? Is there a header row to anchor on?")
    print("=" * 100)
    return 0


def main(argv: list[str]) -> int:
    save_to: str | None = None
    if "--save" in argv:
        i = argv.index("--save")
        try:
            save_to = argv[i + 1]
        except IndexError:
            print("--save needs a path, e.g. --save tests/fixtures/pdf/x.pdf")
            return 2
        argv = argv[:i] + argv[i + 2:]
    sources = [a for a in argv if not a.startswith("--")]
    if not sources:
        print(__doc__)
        return 2
    return inspect(sources[0], save_to)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(1) from None
