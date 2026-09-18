"""Diagnostic: how much RSS does pdfplumber cost on a REAL plan-holder PDF?

Why this exists (2026-09-18): the production backend is being OOM-killed at
its cgroup ceiling (``MemoryMax=4G``, ``constraint=CONSTRAINT_MEMCG`` in the
kernel log) roughly every 2-3 minutes while a research job runs. The journal
shows the process sitting idle-polling for ~70s AFTER the last heavy work —
i.e. RSS peaked during parsing and was never returned to the OS — and the
biggest parse in that window was a 352-page, 1,042,499-char, 53-table PDF
(``airportspecs.pdf``, 1.88 MB on the wire).

Hypothesis under test: pdfplumber builds a Python object per character / line
/ rect, so cost tracks PAGE COUNT and text volume, not byte size — which is
why a 1.88 MB file can be lethal.

This script measures, it does not assert. It prints RSS growth per page and
the peak so the number is on the record either way.

Usage::

    python scripts/pdf_mem_probe.py                     # the observed URL
    python scripts/pdf_mem_probe.py <url-or-path> --max-pages 40
    python scripts/pdf_mem_probe.py <url> --abort-mb 3000
"""

from __future__ import annotations

import argparse
import ctypes
import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: The document the production journal caught the parser reading at 19:00:14.
DEFAULT_URL = (
    "https://dot.alaska.gov/stwddes/dcsspecs/assets/pdf/aptspecs/airportspecs.pdf"
)


# ---------------------------------------------------------------------------
# RSS, without psutil (the repo does not depend on it)
# ---------------------------------------------------------------------------

if os.name == "nt":
    # Imported here, not at module level: ctypes.wintypes raises on Linux and
    # this script has to run on the production box too.
    import ctypes.wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.wintypes.DWORD),
            ("PageFaultCount", ctypes.wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    # argtypes/restype are the whole game here: without them ctypes truncates
    # the HANDLE to a C int, the call fails silently, and every reading is 0.
    _kernel32 = ctypes.windll.kernel32
    _psapi = ctypes.windll.psapi
    _kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
    _psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.wintypes.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL

    def _counters() -> _ProcessMemoryCounters:
        out = _ProcessMemoryCounters()
        out.cb = ctypes.sizeof(out)
        if not _psapi.GetProcessMemoryInfo(
            _kernel32.GetCurrentProcess(), ctypes.byref(out), out.cb
        ):
            raise OSError("GetProcessMemoryInfo failed")
        return out


def _rss_mb() -> float:
    """Current resident set size in MB, on Linux or Windows."""
    if os.name == "nt":
        return _counters().WorkingSetSize / (1024 * 1024)
    with open("/proc/self/status", "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def _peak_rss_mb() -> float:
    """High-water RSS since process start, where the platform reports it."""
    if os.name == "nt":
        return _counters().PeakWorkingSetSize / (1024 * 1024)
    with open("/proc/self/status", "r", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return _rss_mb()


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------

def _load(source: str) -> bytes:
    """The PDF bytes, from a URL or a local path."""
    if source.startswith(("http://", "https://")):
        import httpx

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/pdf,*/*;q=0.8",
        }
        resp = httpx.get(source, headers=headers, timeout=(6.0, 30.0),
                         follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    return Path(source).read_bytes()


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", default=DEFAULT_URL,
                        help="PDF URL or local path")
    parser.add_argument("--max-pages", type=int, default=0,
                        help="stop after N pages (0 = every page)")
    parser.add_argument("--abort-mb", type=float, default=3000.0,
                        help="bail out when RSS crosses this (default 3000)")
    parser.add_argument("--every", type=int, default=25,
                        help="report cadence in pages (default 25)")
    parser.add_argument("--open-only", action="store_true",
                        help="only open the PDF and count pages — proves the "
                             "cheap guard costs nothing next to a real parse")
    parser.add_argument("--flush", action="store_true",
                        help="call page.flush_cache() after each page, to test "
                             "whether pdfplumber's retained page graph is the "
                             "thing that grows")
    args = parser.parse_args()

    import pdfplumber

    base = _rss_mb()
    print(f"baseline RSS            : {base:8.1f} MB")
    print(f"source                  : {args.source}")

    t0 = time.time()
    try:
        data = _load(args.source)
    except Exception as exc:  # noqa: BLE001 - a probe reports, never crashes
        print(f"FETCH FAILED            : {type(exc).__name__}: {exc}")
        return 2
    after_fetch = _rss_mb()
    print(f"bytes on the wire       : {len(data):,}  "
          f"({len(data) / 1024 / 1024:.2f} MB)")
    print(f"RSS after download      : {after_fetch:8.1f} MB "
          f"(+{after_fetch - base:.1f})")
    print()

    cap = args.max_pages or 0
    pages_done = 0
    tables_seen = 0
    text_chars = 0
    aborted: str | None = None

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages)
        print(f"page count              : {total}")

        if args.open_only:
            print(f"RSS after len(pages)    : {_rss_mb():8.1f} MB "
                  f"(+{_rss_mb() - base:.1f})")
            print(f"PEAK RSS (high-water)   : {_peak_rss_mb():8.1f} MB")
            print(f"elapsed                 : {time.time() - t0:.1f}s")
            print("=== open+count only — no page was ever extracted ===")
            return 0

        print(f"{'page':>6}  {'RSS MB':>9}  {'delta':>8}  "
              f"{'chars':>9}  {'tables':>6}")
        print("-" * 50)

        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            pages_done = page_number
            text_chars += len(text)
            tables_seen += len(tables)

            rss = _rss_mb()
            if args.flush:
                page.flush_cache()
                rss = _rss_mb()
            if page_number % args.every == 0 or page_number == total:
                print(f"{page_number:>6}  {rss:>9.1f}  {rss - base:>+8.1f}  "
                      f"{text_chars:>9,}  {tables_seen:>6}")

            if rss >= args.abort_mb:
                aborted = (f"ABORTED at page {page_number}: RSS {rss:.1f} MB "
                           f"crossed --abort-mb {args.abort_mb:.0f}")
                break
            if cap and page_number >= cap:
                aborted = f"stopped at --max-pages {cap}"
                break

    elapsed = time.time() - t0
    print()
    print("=" * 50)
    print(f"pages parsed            : {pages_done}")
    print(f"text chars              : {text_chars:,}")
    print(f"tables seen             : {tables_seen}")
    print(f"elapsed                 : {elapsed:.1f}s")
    print(f"final RSS               : {_rss_mb():8.1f} MB")
    print(f"PEAK RSS (high-water)   : {_peak_rss_mb():8.1f} MB")
    print(f"peak over baseline      : {_peak_rss_mb() - base:8.1f} MB")
    if aborted:
        print(f"note                    : {aborted}")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
