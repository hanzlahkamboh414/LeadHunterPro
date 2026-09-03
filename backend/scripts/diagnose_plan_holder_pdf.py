"""Run the REAL plan-holder parser over a PDF and report what it produced.

WHY THIS EXISTS, SEPARATELY FROM inspect_pdf.py
-----------------------------------------------
``inspect_pdf.py`` answers "what does this document physically contain?" — raw
text, raw tables, raw cells. It is a document tool and knows nothing about
LeadHunter.

This script answers the different, and after implementation the more important,
question: "what does OUR PARSER make of it?" It imports
:class:`PdfPlanHolderParser` and prints its actual output, so the thing being
inspected is the production code path rather than a reimplementation of it. A
diagnostic that reimplements the parser drifts from the parser, and then lies —
which is the specific failure these tools exist to catch.

It is also the verification artifact for this increment: one shell-agnostic
command, identical in PowerShell, CMD and Git Bash, that prints the parse
report (every count from the CLAUDE.md §6 logging standard) plus every row.

Emails and phones are MASKED in the printed output, because this output gets
pasted into chat logs. The fetch and masking helpers are imported from
``inspect_pdf`` rather than copied (rule #14: reuse before rewrite).

USAGE, from ``backend/``::

    python scripts/diagnose_plan_holder_pdf.py                  # committed fixture
    python scripts/diagnose_plan_holder_pdf.py <url>
    python scripts/diagnose_plan_holder_pdf.py path/to/local.pdf
    python scripts/diagnose_plan_holder_pdf.py <url> --unmasked  # local eyes only

Exit code is 0 only when the parse status is ``ok``, so this is usable as a gate.
"""

from __future__ import annotations

import pathlib
import sys

# Same two-bootstrap trick as scripts/inspect_pdf.py: backend/ on the path for
# `import app`, and this dir on the path for the sibling script imports.
_HERE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))
sys.path.insert(0, str(_HERE.parent))

import inspect_pdf  # noqa: E402  - sibling diagnostic, reused for fetch + masking

from app.discovery.pdf_plan_holder_parser import (  # noqa: E402
    STATUS_OK,
    PdfPlanHolderParser,
    PlanHolderParseResult,
)
from app.engines.lead.lead_models import EmailVerificationTier  # noqa: E402

#: Default target: the committed fixture, so the command works with no argument
#: and no network.
DEFAULT_SOURCE = str(
    _HERE.parents[1] / "tests" / "fixtures" / "pdf" / "hrgreen_plan_holder_list.pdf"
)

_RULE = "=" * 100
_THIN = "-" * 100


def _show(text: str, *, unmasked: bool) -> str:
    """Mask emails/phones unless the operator explicitly opted out."""
    return text if unmasked else inspect_pdf._mask(text)  # noqa: SLF001


def _print_report(result: PlanHolderParseResult) -> None:
    """Print every field of the parse report, the §6 logging standard on paper."""
    report = result.report
    print(_THIN)
    print("  PARSE REPORT")
    print(_THIN)
    print(f"  Status              : {report.parse_status}")
    print(f"  Pages               : {report.pages}")
    print(f"  Text chars          : {report.text_chars:,}")
    print(
        f"  Tables              : {report.tables_seen} seen, "
        f"{report.tables_used} used, {report.tables_discarded} discarded"
    )
    print(
        f"  Rows                : {report.rows_extracted} extracted, "
        f"{report.rows_rejected} rejected"
    )
    print(
        f"  Email coverage      : {report.emails_captured} captured of "
        f"{report.emails_in_document} in document ({report.coverage_ratio:.0%})"
    )
    print(
        f"  Phone coverage      : {report.phones_captured} captured of "
        f"{report.phones_in_document} in document "
        f"({report.phone_coverage_ratio:.0%})"
    )
    if report.unmatched_phone_shapes:
        print("  Unmatched shapes    : " + ", ".join(report.unmatched_phone_shapes))
        print("                        (digit-masked - these are the formats")
        print("                         extraction is currently blind to)")
    if report.reasons:
        for reason in report.reasons:
            print(f"  REASON              : {reason}")
    else:
        print("  Reasons             : (none - clean parse)")


def _print_rows(result: PlanHolderParseResult, *, unmasked: bool) -> None:
    """Print one block per extracted row, with its flags."""
    print()
    print(_THIN)
    print(f"  ROWS ({len(result.rows)})")
    print(_THIN)
    for index, row in enumerate(result.rows, start=1):
        person = row.person.name if row.person else "(no name in cell)"
        flags = []
        if row.free_mail_only:
            flags.append("FREE-MAIL-ONLY (no company domain)")
        if row.emails and not row.local_part_matches_name:
            flags.append("local-part != name (shared inbox?)")
        if not row.phones:
            flags.append("no phone")
        if row.person is None:
            flags.append("no person")

        print(f"  {index:>3}. p{row.page}  {_show(row.company, unmasked=unmasked)}")
        print(f"       contact : {_show(person, unmasked=unmasked)}")
        for email in row.emails:
            print(
                f"       email   : {_show(email.email, unmasked=unmasked)}"
                f"   [{email.tier.value}]"
            )
        for phone, label in zip(row.phones, row.phone_labels, strict=True):
            shown = _show(phone.phone, unmasked=unmasked)
            print(f"       phone   : {shown}   [{label or 'unlabelled'}]")
        if row.date_contacted:
            print(f"       dated   : {row.date_contacted}")
        if flags:
            print(f"       flags   : {'; '.join(flags)}")


def _print_summary(result: PlanHolderParseResult) -> None:
    """Aggregate the qualification-relevant facts across all rows."""
    rows = result.rows
    person_bound = sum(
        1
        for row in rows
        for email in row.emails
        if email.tier == EmailVerificationTier.person_bound
    )
    format_tier = sum(
        1
        for row in rows
        for email in row.emails
        if email.tier == EmailVerificationTier.format
    )
    print()
    print(_THIN)
    print("  SUMMARY")
    print(_THIN)
    print(f"  Rows with a named person : {sum(1 for r in rows if r.person)}/{len(rows)}")
    print(f"  Rows with a phone        : {sum(1 for r in rows if r.phones)}/{len(rows)}")
    print(f"  Emails person_bound      : {person_bound}")
    print(f"  Emails format (generic)  : {format_tier}")
    print(f"  Free-mail-only rows      : {sum(1 for r in rows if r.free_mail_only)}")
    print(f"  Distinct companies       : {len({r.company for r in rows})}")
    print()
    print("  NOTE: every person here has role='' and role_relevance=False - a")
    print("  planholder list carries no job title. These rows are a sourcing feed;")
    print("  they cannot clear the V1 qualification gate until a later increment")
    print("  enriches the role (hard rule #2).")


def diagnose(source: str, *, unmasked: bool) -> int:
    """Fetch/read ``source``, parse it, print the report and rows."""
    print(_RULE)
    print(f"DIAGNOSE PLAN-HOLDER PDF: {source}")
    print(_RULE)

    try:
        data, note = inspect_pdf._load_bytes(source, None)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - report the failure, do not traceback
        print(f"  COULD NOT LOAD: {exc}")
        if source == DEFAULT_SOURCE:
            print("  The committed fixture is missing. Pass a URL or a local path,")
            print("  or re-save it with:")
            print("      python scripts/inspect_pdf.py <url> --save " + DEFAULT_SOURCE)
        return 2
    print(f"  Source      : {note}")

    source_url = source if source.startswith(("http://", "https://")) else ""
    result = PdfPlanHolderParser().parse(data, source_url=source_url)

    _print_report(result)
    if result.rows:
        _print_rows(result, unmasked=unmasked)
        _print_summary(result)

    print()
    print(_RULE)
    verdict = "PASS" if result.report.parse_status == STATUS_OK else "ATTENTION"
    print(f"  {verdict} - status={result.report.parse_status}")
    print(_RULE)
    return 0 if result.report.parse_status == STATUS_OK else 1


def main(argv: list[str]) -> int:
    unmasked = "--unmasked" in argv
    positional = [arg for arg in argv if not arg.startswith("--")]
    source = positional[0] if positional else DEFAULT_SOURCE
    return diagnose(source, unmasked=unmasked)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(1) from None
