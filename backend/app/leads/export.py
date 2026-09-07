"""Leads API — CSV export of researched leads (zero external deps).

Produces a flat CSV the sales team / frontend can open in Excel/Sheets.
Two export modes:
- Default: email + name only (for outreach lists)
- Full: all columns (for detailed analysis)
"""

from __future__ import annotations

import csv
import io
from typing import Any, Iterable

# Default export: email + name only (user request: "khali emails hi rehne do or name bas")
_COLUMNS_EMAIL_NAME = [
    "email",
    "name",
    "company",
]

# Full export: all columns (for detailed analysis)
_COLUMNS_FULL = [
    "email",
    "domain",
    "refined_domain",
    "company",
    "website",
    "person",
    "role",
    "bound",
    "score",
    "recommendation",
    "intent",
    "timing",
    "fit",
]


def _row_email_name(d: Any) -> list[str]:
    """Flatten one LeadDossier into email+name CSV row."""
    return [
        d.email or "",
        d.person.name or "",
        d.company.name or "",
    ]


def _row_full(d: Any, rec: str = "") -> list[str]:
    """Flatten one LeadDossier into a full CSV row.

    ``rec`` is the CURRENT deterministic recommendation (re-gated at read time)
    — a stale AI-era label is never exported as-is.
    """
    return [
        d.email or "",
        d.domain or "",
        d.refined_domain or "",
        d.company.name or "",
        d.company.website or "",
        d.person.name or "",
        d.person.role or "",
        "yes" if d.person.bound else "no",
        _num(d.potential_score),
        rec or d.recommendation or "",
        d.intent.needs_estimation if d.intent else "",
        d.timing.window if d.timing else "",
        d.fit or "",
    ]


def _num(v: Any) -> str:
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return ""


def dossiers_to_csv(
    dossiers: Iterable[Any],
    full: bool = False,
    rec_of: dict[str, str] | None = None,
) -> str:
    """Render a list of LeadDossiers as a CSV string (header + rows).

    ``rec_of`` maps ``email -> current deterministic recommendation`` (re-gated
    at read time); when absent, the stored label is used as-is.
    """
    rec_of = rec_of or {}
    buf = io.StringIO()
    writer = csv.writer(buf)
    if full:
        writer.writerow(_COLUMNS_FULL)
        for d in dossiers:
            writer.writerow(_row_full(d, rec_of.get(d.email, "")))
    else:
        writer.writerow(_COLUMNS_EMAIL_NAME)
        for d in dossiers:
            writer.writerow(_row_email_name(d))
    return buf.getvalue()


def export_csv(
    store: Any,
    recommendation: str | None = None,
    emails: list[str] | None = None,
    full: bool = False,
) -> str:
    """Export persisted leads to CSV.

    ``store`` is a LeadResearchStore (its ``list_all`` returns dossiers).
    ``emails`` — if provided, only export these specific emails.
    ``full`` — if True, export all columns; if False, email+name only.
    """
    from app.lead_research.scoring import regate_recommendation

    dossiers = store.list_all()
    rec_map: dict[str, str] = {}
    for d in dossiers:
        rec_map[d.email] = regate_recommendation(d)
    if emails:
        email_set = {e.lower() for e in emails}
        dossiers = [d for d in dossiers if d.email.lower() in email_set]
    elif recommendation:
        dossiers = [
            d for d in dossiers if rec_map.get(d.email, d.recommendation) == recommendation
        ]
    else:
        # Honest default — mirror the leads list: skip/junk is NOT exported
        # unless explicitly requested (a dead domain is not a lead, and its
        # row inflates the outreach list). `?recommendation=skip` still gets
        # every skip row, exactly as the filter allows.
        dossiers = [
            d for d in dossiers if rec_map.get(d.email, d.recommendation) != "skip"
        ]
    return dossiers_to_csv(dossiers, full=full, rec_of=rec_map)
