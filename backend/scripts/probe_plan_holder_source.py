"""Probe: run the REAL PlanHolderSource live and report what it found.

VERIFICATION ARTIFACT for Inc 2. One shell-agnostic command (identical in
PowerShell, CMD and Git Bash) that drives the production source through the
live search registry and the real fetch/parse path — the thing that cannot
be exercised offline.

WHY THIS IS THE ARTIFACT, NOT A ONE-LINER
    The dork probe (``probe_search_dork.py``) already proved the search side.
    ``diagnose_plan_holder_pdf.py`` already proved the parser. This script
    proves the CONNECTION: search -> .pdf filter -> fetch -> parse -> records,
    through ``PlanHolderSource.discover`` itself, so the module under
    inspection is the production code path (rule: a diagnostic that
    reimplements the code drifts from it, then lies).

PII SAFETY
    Output is pasted into chat logs, so emails are MASKED (reusing
    ``inspect_pdf._mask``). Company names, person names and the query print
    as-is. None of these are private — they are public bid records — but the
    mask keeps a pasted log free of raw addresses.

USAGE, from ``backend/``::

    python scripts/probe_plan_holder_source.py
    python scripts/probe_plan_holder_source.py "Roofing" "Texas" 20

Exit code 0 only when the source returned at least one company record.
"""

from __future__ import annotations

import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))  # backend/ on path for `import app`
sys.path.insert(0, str(_HERE.parent))  # this dir for the sibling inspect_pdf

import inspect_pdf  # noqa: E402 - reused for masking only

from app.discovery.sources.plan_holder_source import PlanHolderSource  # noqa: E402

_RULE = "=" * 100
_THIN = "-" * 100


def main(argv: list[str]) -> int:
    industry = argv[0] if len(argv) > 0 else "Roofing"
    location = argv[1] if len(argv) > 1 else "Texas"
    limit = int(argv[2]) if len(argv) > 2 else 20

    source = PlanHolderSource()
    dorks = source._build_dorks(industry, location)  # noqa: SLF001 - the source under test

    print(_RULE)
    print(f"PROBE PLAN-HOLDER SOURCE (live): industry={industry!r} location={location!r} limit={limit}")
    print(_RULE)
    print("  Dorks:")
    for dork in dorks:
        print(f"    - {dork}")

    start = time.monotonic()
    status, records, meta = source.discover(industry=industry, location=location, limit=limit)
    elapsed = time.monotonic() - start

    print(_THIN)
    print(f"  Status           : {status.value}")
    print(f"  Elapsed          : {elapsed:.1f}s")
    print(f"  PDFs             : found={meta.get('pdfs_found')}  "
          f"fetched={meta.get('pdfs_fetched')}  failed={meta.get('pdfs_failed')}  "
          f"unreadable={meta.get('pdfs_unreadable')}")
    print(f"  Rows             : raw={meta.get('rows_raw')}  emitted={meta.get('rows_emitted')}")
    if meta.get("reason"):
        print(f"  Reason           : {meta['reason']}")

    if not records:
        print(_THIN)
        print("  No company records emitted. Reading this:")
        print("   - no_pdf_results    -> no .pdf URL surfaced for these dorks from this machine")
        print("   - all_fetches_failed-> PDFs found but every fetch failed (DNS/network)")
        print("   - no_rows_in_pdfs   -> PDFs fetched but no plan-holder rows (scan/layout)")
        print("   None of these means no plan-holder list exists (CLAUDE.md §7).")
        print(_RULE)
        return 1

    print(_THIN)
    print(f"  RECORDS ({len(records)})  (emails masked)")
    print(_THIN)
    for index, rec in enumerate(records, start=1):
        holder = rec.get("plan_holder") or {}
        person = holder.get("person") or {}
        emails = [
            inspect_pdf._mask(e["email"])  # noqa: SLF001 - masking helper
            for e in holder.get("emails") or []
        ]
        role = person.get("role") or ""
        rel = person.get("role_relevance")
        flags = []
        if holder.get("free_mail_only"):
            flags.append("FREE-MAIL")
        if not role:
            flags.append("role='' (no title in list)")
        if not rel:
            flags.append("role_relevance=False (V1 gate still blocked on role)")
        print(f"  {index:>3}. {rec['company_name']}")
        print(f"       person  : {person.get('name') or '(none)'}   role={role!r}  rel={rel}")
        print(f"       emails  : {', '.join(emails) if emails else '(none)'}")
        print(f"       website : {rec.get('website') or '(none - free-mail or no domain)'}")
        if flags:
            print(f"       flags   : {'; '.join(flags)}")

    print(_RULE)
    verdict = "PASS" if status.value == "success" else "ATTENTION"
    print(f"  {verdict} - {len(records)} company record(s) from live plan-holder PDFs")
    print(_RULE)
    return 0 if status.value == "success" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(1) from None
