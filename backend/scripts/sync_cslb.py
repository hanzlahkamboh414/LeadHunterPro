"""CSLB bulk-sync — the browser driver that stocks ``cslb_portal`` rows.

Why a BROWSER driver and not a connector: CSLB's F5 edge rejects every
scripted transport (verified 2026-09-14 — even a replayed real-browser
request fails; the edge fingerprints TLS/JA3 + HTTP/2 frames) and
rate-blocks rapid sessions (504 after ~3 in 10 minutes). The only
automatable path is a HEADED real Chrome via patchright with a
PERSISTENT profile (the profile accumulates F5 trust between runs),
one classification download at a time, spaced out by --delay-secs.

What it does, per classification code in CSLB_CODE_MAP:

    goto portal -> wait for F5 clearance + the form -> select the code
    -> click Search (the response IS an xlsx download) -> parse_xlsx
    -> PhoneLeadsStore.add (folds the label to the slug, dedupes).

Honesty rules (this is a maintenance script, but the project rules hold):

  * F5 block detected -> ABORT with the list of remaining codes, so a
    re-run with --only=<those> resumes after the rate-limit cooldown.
  * Every code gets a summary row (rows parsed / inserted / duplicate /
    dropped / suppressed / error) printed as JSON at the end — never a
    silent skip.
  * Re-runs are idempotent: the store dedupes on (phone, business_name),
    so syncing the same classification again inserts nothing new.

Run from backend/ (the app package import + default DB path assume it):

    python scripts/sync_cslb.py                 # every classification
    python scripts/sync_cslb.py --only=B,C-10   # a subset (resume)
    python scripts/sync_cslb.py --delay-secs=120

The fetch lane NEVER touches this source — fetch_license_records refuses
``cslb_portal`` honestly; this script is the only writer of its rows.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from app.phones.cslb import (
    CSLB_CODE_MAP,
    PORTAL_URL,
    SOURCE_ID,
    parse_xlsx,
)
from app.phones.store import PhoneLeadsStore

#: The persistent Chrome profile — output/ is gitignored, and reusing one
#: profile is the point (F5 trust accumulates between runs).
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(BACKEND_ROOT, "output", "cslb_profile")

#: Requests this far apart stay under the observed rate limit (3 rapid
#: sessions got 504-blocked; ~90s spacing is comfortably human).
DEFAULT_DELAY_S = 90.0

_SELECT = 'select[name="ctl00$MainContent$lbClassification"]'

#: Markers of the F5 interstitial / rate-limit block (live captures,
#: 2026-09-14). Seeing one means STOP — retries from the same session
#: make the block worse.
_F5_MARKERS = (
    "the requested url was rejected",
    "error 504",
)


def _log(msg: str) -> None:
    print(f"[sync_cslb] {msg}", flush=True)


def _f5_blocked(page) -> bool:
    """True when the page is the F5 rejection interstitial, not the form."""
    try:
        html = (page.content() or "").lower()
    except Exception:  # noqa: BLE001 — a dead page is not a crash here
        return False
    return any(marker in html for marker in _F5_MARKERS)


def _wait_for_form(page, context, timeout_s: float = 120.0) -> str:
    """Wait until the F5 JS challenge finished and the form is usable.

    Returns "" on success, else an honest reason. Clearance = the
    classification select is rendered AND the waitingCookie challenge (if
    any) reports done.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        try:
            has_form = page.query_selector(_SELECT) is not None
        except Exception:  # noqa: BLE001 — page may navigate mid-poll
            has_form = False
        if not has_form:
            continue
        cookies = {c["name"]: c.get("value", "") for c in context.cookies()}
        if cookies.get("waitingCookie", "done") == "done":
            return ""
        # Form rendered but the challenge is still running — keep waiting.
    return "F5 clearance/form did not become ready in time"


def _select_classification(page, code: str) -> str:
    """Select one classification value; '' on success, else a reason."""
    try:
        page.select_option(_SELECT, code, force=True, timeout=15000)
        return ""
    except Exception as exc:  # noqa: BLE001 — JS fallback is the fix
        _log(f"select_option fallback for {code}: {str(exc)[:120]}")
    try:
        page.evaluate(
            "code => {"
            "const el = document.getElementById('lbClassification');"
            "el.value = code;"
            "el.dispatchEvent(new Event('change', {bubbles: true}));"
            "return el.value;}",
            code,
        )
        return ""
    except Exception as exc:  # noqa: BLE001 — honest error, never a guess
        return f"could not select {code}: {str(exc)[:160]}"


def _download_xlsx(page, timeout_s: float = 240.0) -> tuple[bytes, str]:
    """Click Search and capture the xlsx download; (data, '') or (b'', why)."""
    try:
        with page.expect_download(timeout=timeout_s) as dl_info:
            page.evaluate(
                "document.getElementById('btnSearch').click()")
        dl = dl_info.value
        with open(dl.path(), "rb") as fh:
            return fh.read(), ""
    except Exception as exc:  # noqa: BLE001 — timeout/abort is a reason
        return b"", f"no download: {str(exc)[:200]}"


def sync(store: PhoneLeadsStore, codes: list[str], *, delay_s: float,
         headless: bool = False) -> list[dict]:
    """Drive the browser over ``codes``; returns the honest per-code summary.

    ``codes`` are CSLB_CODE_MAP keys (validated by the caller).
    """
    from patchright.sync_api import sync_playwright

    results: list[dict] = []
    remaining = list(codes)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=headless,
            channel="chrome",
            viewport={"width": 1366, "height": 900},
            accept_downloads=True,
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            for i, code in enumerate(codes):
                slug, label = CSLB_CODE_MAP[code]
                entry: dict = {
                    "code": code, "slug": slug, "label": label,
                    "rows": 0, "inserted": 0, "duplicate": 0,
                    "dropped": 0, "suppressed": 0, "error": "",
                }
                remaining.remove(code)
                try:
                    page.goto(PORTAL_URL, wait_until="domcontentloaded",
                              timeout=120000)
                    if _f5_blocked(page):
                        entry["error"] = "f5_block"
                        results.append(entry)
                        _abort(remaining, results)
                        return results
                    reason = _wait_for_form(page, context)
                    if reason:
                        entry["error"] = reason
                        if _f5_blocked(page):
                            entry["error"] = "f5_block"
                            results.append(entry)
                            _abort(remaining, results)
                            return results
                        results.append(entry)
                        continue
                    reason = _select_classification(page, code)
                    if reason:
                        entry["error"] = reason
                        results.append(entry)
                        continue
                    data, why = _download_xlsx(page)
                    if not data:
                        entry["error"] = (
                            "f5_block" if _f5_blocked(page) else why)
                        results.append(entry)
                        if entry["error"] == "f5_block":
                            _abort(remaining, results)
                            return results
                        continue
                    records = parse_xlsx(data, label=label)
                    entry["rows"] = len(records)
                    counts = store.add(records)
                    entry.update({
                        "inserted": counts["inserted"],
                        "duplicate": counts["duplicate"],
                        "dropped": counts["dropped_bad_phone"],
                        "suppressed": counts["suppressed"],
                    })
                except Exception as exc:  # noqa: BLE001 — one code never kills the run
                    entry["error"] = f"exception: {str(exc)[:200]}"
                results.append(entry)
                _log(
                    f"{code} ({slug}): rows={entry['rows']} "
                    f"inserted={entry['inserted']} dup={entry['duplicate']} "
                    f"dropped={entry['dropped']} err={entry['error'] or '-'}"
                )
                # Space requests out — the rate limit is the whole reason
                # this is a bulk sync, not a fetch lane.
                if i < len(codes) - 1:
                    _log(f"sleeping {delay_s:.0f}s before the next request…")
                    time.sleep(delay_s)
        finally:
            context.close()
    return results


def _abort(remaining: list[str], results: list[dict]) -> None:
    """Honest abort on an F5 block: say what is left and how to resume."""
    _log(
        "F5 BLOCK detected — stopping (rapid retries make the block worse). "
        f"Remaining codes: {','.join(remaining) or '(none)'}. "
        "Resume after a cooldown with: "
        f"python scripts/sync_cslb.py --only={','.join(remaining)}"
        if remaining else
        "F5 BLOCK detected — stopping (no codes remained anyway)."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-sync CSLB classifications into the phone pool "
                    "(headed real browser — the F5 edge allows nothing else).")
    parser.add_argument(
        "--only", default="",
        help="comma-separated classification codes (e.g. B,C-10); default "
             "is every code in CSLB_CODE_MAP")
    parser.add_argument(
        "--delay-secs", type=float, default=DEFAULT_DELAY_S,
        help=f"seconds between downloads (default {DEFAULT_DELAY_S:.0f}) — "
             "the rate-limit guard")
    parser.add_argument(
        "--db", default="",
        help="phone_leads.db path (default: the store's output/ default)")
    parser.add_argument(
        "--headless", action="store_true",
        help="headless Chrome (NOT recommended — HeadlessChrome UA is the "
             "proven F5 bot signal)")
    args = parser.parse_args()

    if args.only:
        wanted = [c.strip() for c in args.only.split(",") if c.strip()]
        unknown = [c for c in wanted if c not in CSLB_CODE_MAP]
        if unknown:
            _log(f"unknown classification code(s) {unknown}; valid: "
                 f"{sorted(CSLB_CODE_MAP)}")
            return 2
        codes = wanted
    else:
        codes = sorted(CSLB_CODE_MAP)

    store = PhoneLeadsStore(
        db_path=args.db or None)  # '' -> the store's own default path
    _log(
        f"source={SOURCE_ID} codes={codes} delay={args.delay_secs:.0f}s "
        f"headless={args.headless} (profile: {PROFILE_DIR})"
    )
    results = sync(store, codes, delay_s=max(0.0, args.delay_secs),
                   headless=args.headless)

    failed = [r for r in results if r["error"]]
    print(json.dumps({
        "source": SOURCE_ID,
        "codes_attempted": len(results),
        "codes_ok": len(results) - len(failed),
        "rows_parsed": sum(r["rows"] for r in results),
        "rows_inserted": sum(r["inserted"] for r in results),
        "failures": failed,
        "results": results,
    }, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
