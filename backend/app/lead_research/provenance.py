"""Evidence provenance — WHERE exactly each fact was observed.

Why this module exists (root cause, CLAUDE.md §7): the AI emitted every
``source_url`` as the company-site ROOT because the research pipeline handed
it six ANONYMOUS text blocks — the model had no way to know which block was
``/about`` and which was ``/contact``, so the only URL it could honestly cite
was the bare domain it already knew. The user's exact complaint: "har evidence
sirf company website ka direct link deta hai... ye batata nahi k ye info about
se li ya jahan se b exact li."

This module (shared by company + person research — CLAUDE.md §14, no
duplication):

1. LABELS every gathered page block with its real URL + a human name, so the
   AI can cite ``https://acme.com/about`` exactly instead of the root.
2. DETECTS the company's LinkedIn page already surfaced in search results and
   turns a (Tavily ``/extract``) read of it into another labeled block —
   the LinkedIn activity lane.
3. GUARDS the parsed facts: a "verified" claim whose source is a bare domain
   root with no location reported is demoted to ``unverified`` with the
   honest reason in ``source_note`` — a claim with no exact location cannot
   claim verification.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable
from urllib.parse import urlparse

from app.lead_research.models import AIEvidence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Labeled page blocks
# ---------------------------------------------------------------------------

#: Known page paths -> (human label, source_type). Unknown paths fall back to
#: ("Web page", "website_other") — still LABELED with the real URL, never a
#: guessed name that could mislead the person citing it.
_PAGE_LABELS: dict[str, tuple[str, str]] = {
    "/": ("Homepage", "homepage"),
    "/about": ("About us", "about_page"),
    "/about-us": ("About us", "about_page"),
    "/aboutus": ("About us", "about_page"),
    "/our-company": ("About us", "about_page"),
    "/company": ("About us", "about_page"),
    "/contact": ("Contact", "contact_page"),
    "/contact-us": ("Contact", "contact_page"),
    "/team": ("Team page", "team_page"),
    "/our-team": ("Team page", "team_page"),
    "/leadership": ("Team page", "team_page"),
    "/people": ("Team page", "team_page"),
    "/staff": ("Team page", "team_page"),
    "/services": ("Services page", "services_page"),
    "/projects": ("Projects page", "projects_page"),
    "/careers": ("Careers page", "careers_page"),
    "/jobs": ("Careers page", "careers_page"),
}

#: LinkedIn "company activity" URLs — the company page, NOT a person profile.
_LINKEDIN_COMPANY_RE = re.compile(
    r"(?:https?://)?(?:[a-z]+\.)?linkedin\.com/company/[A-Za-z0-9_\-\%\.]+/?",
    re.IGNORECASE,
)

#: LinkedIn person-profile URLs (``/in/``).
_LINKEDIN_PROFILE_RE = re.compile(
    r"(?:https?://)?(?:[a-z]+\.)?linkedin\.com/in/[A-Za-z0-9_\-\.%]+",
    re.IGNORECASE,
)


def _truncate_to_text(html: str, max_chars: int = 4000) -> str:
    """Strip script/style + tags, collapse whitespace, bound length."""
    cleaned = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_chars]


def page_label(url: str) -> tuple[str, str]:
    """(human label, source_type) for a known page path.

    Unknown paths return ("", "") so the caller falls back to a generic
    "Web page" label — never a guessed description of what the page is.
    """
    try:
        path = urlparse(url).path.rstrip("/") or "/"
    except ValueError:
        return "", ""
    return _PAGE_LABELS.get(path, ("", ""))


def labeled_page_block(url: str, html: str, *, max_chars: int = 4000) -> str:
    """One page's text, labeled with its REAL URL + human name.

    ``[PAGE: About us — https://acme.com/about]`` followed by the text, so
    the AI can cite the exact page a claim came from. Returns "" when the
    page yielded nothing readable (the page is then just absent — honest).
    """
    text = _truncate_to_text(html, max_chars=max_chars)
    if not text:
        return ""
    label, _ = page_label(url)
    label = label or "Web page"
    return f"[PAGE: {label} — {url}]\n{text}\n"


def find_linkedin_company_url(results: list[dict[str, Any]]) -> str:
    """First ``linkedin.com/company/<slug>`` URL surfaced in search results.

    Only a URL the pipeline ACTUALLY saw counts — never a fabricated handle
    (CLAUDE.md §12) and never a person profile (``/in/``).
    """
    for r in results:
        match = _LINKEDIN_COMPANY_RE.search((r.get("url") or ""))
        if match:
            return match.group(0).rstrip("/")
    return ""


def find_linkedin_profile_url(results: list[dict[str, Any]]) -> str:
    """First ``linkedin.com/in/<handle>`` URL surfaced in search results."""
    for r in results:
        match = _LINKEDIN_PROFILE_RE.search((r.get("url") or ""))
        if match:
            return match.group(0)
    return ""


def linkedin_block(url: str, extract: Callable[[str], str], *, max_chars: int = 4000) -> str:
    """Extract + label one LinkedIn page into a prompt block.

    The (Tavily ``/extract``) read of a public LinkedIn page becomes a labeled
    block exactly like a website page — the AI can then cite
    ``linkedin.com/company/<slug>`` for activity/news/hiring observed there.
    An empty extract (login wall, provider without the capability) returns ""
    — the lane is additive, and a walled page is never fabricated into
    activity the AI did not see.
    """
    if not (url or "").strip():
        return ""
    try:
        text = (extract(url) or "").strip()
    except Exception:  # noqa: BLE001 — an uncooperative page is not fatal
        text = ""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return f"[PAGE: LinkedIn — {url}]\n{text[:max_chars]}\n"


# ---------------------------------------------------------------------------
# LinkedIn lane gate
# ---------------------------------------------------------------------------

def linkedin_lane_enabled() -> bool:
    """Config gate for the LinkedIn extract lane.

    On by default; ``LINKEDIN_LANE_ENABLED=0`` turns it off. Without a
    provider key for extraction it is a silent no-op below (block returns ""),
    but the intent is explicit in config either way.
    """
    return os.environ.get("LINKEDIN_LANE_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


# ---------------------------------------------------------------------------
# Location guard — exact-page teeth
# ---------------------------------------------------------------------------

def _is_bare_root(url: str) -> bool:
    """True for a bare-origin URL (``https://acme.com`` or ``https://acme.com/``).

    A web search result or a page citation nearly always carries a path of
    some kind; a bare root is the lazy default the AI reaches for when it
    cannot name the exact page.
    """
    if not (url or "").strip():
        return False
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        return False
    return parsed.path in ("", "/")


def guard_evidence_location(facts: list[AIEvidence]) -> list[AIEvidence]:
    """Demote "verified" claims that name no exact page.

    A claim whose ``source_url`` is a bare domain root — and whose
    ``source_type`` is not explicitly a homepage citation — cannot be
    attributed to a specific page, so it must not claim verification. The
    honest reason is recorded in ``source_note``; the claim and URL survive
    (unverified, not deleted). This is the enforcement behind the exact-page
    rule: "jahan se li woh batao, warna verified nahi."
    """
    out: list[AIEvidence] = []
    for f in facts:
        st = (f.source_type or "").strip().lower()
        src = (f.source_url or "").strip()
        if (
            f.confidence == "verified"
            and (not src or (_is_bare_root(src) and st != "homepage"))
        ):
            logger.info(
                "Location guard: '%.60s' demoted to unverified — source %r names no exact page",
                f.claim, f.source_url,
            )
            out.append(AIEvidence(
                claim=f.claim,
                source_url=f.source_url,
                source_type=f.source_type,
                confidence="unverified",
                source_note=f.source_note or "source location not reported",
            ))
        else:
            out.append(f)
    return out