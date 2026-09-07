"""provenance.py — exact-page labels, LinkedIn lane helpers, location guard."""

from __future__ import annotations

import pytest

from app.lead_research.models import AIEvidence
from app.lead_research.provenance import (
    find_linkedin_company_url,
    find_linkedin_profile_url,
    guard_evidence_location,
    labeled_page_block,
    linkedin_block,
    linkedin_lane_enabled,
    page_label,
)


# ---------------------------------------------------------------------------
# page_label / labeled_page_block
# ---------------------------------------------------------------------------

def test_page_label_known_paths():
    assert page_label("https://acme.com/") == ("Homepage", "homepage")
    assert page_label("https://acme.com/about") == ("About us", "about_page")
    assert page_label("https://acme.com/contact-us") == ("Contact", "contact_page")
    assert page_label("https://acme.com/team") == ("Team page", "team_page")
    assert page_label("https://acme.com/jobs") == ("Careers page", "careers_page")


def test_page_label_unknown_path_is_blank():
    # Unknown path -> ("", "") — the caller falls back to "Web page", never
    # a guessed description that could mislead.
    assert page_label("https://acme.com/weird-page") == ("", "")


def test_labeled_page_block_stamps_exact_url():
    block = labeled_page_block("https://acme.com/about", "<p>We build things</p>")
    assert "[PAGE: About us — https://acme.com/about]" in block
    assert "We build things" in block


def test_labeled_page_block_falls_back_to_web_page():
    block = labeled_page_block("https://acme.com/news/2026", "<p>Fresh news</p>")
    assert "[PAGE: Web page — https://acme.com/news/2026]" in block


def test_labeled_page_block_empty_html_returns_empty():
    assert labeled_page_block("https://acme.com/about", "<script></script>") == ""
    assert labeled_page_block("https://acme.com/about", "") == ""


# ---------------------------------------------------------------------------
# LinkedIn URL detection
# ---------------------------------------------------------------------------

def test_find_linkedin_company_url_matches_company_pages():
    results = [
        {"url": "https://example.com/about", "title": "", "snippet": ""},
        {"url": "https://www.linkedin.com/company/acme-construction", "title": "Acme", "snippet": ""},
    ]
    assert find_linkedin_company_url(results) == "https://www.linkedin.com/company/acme-construction"


def test_find_linkedin_company_url_ignores_profiles():
    """A person profile (/in/) MUST NOT be treated as the company page."""
    results = [{"url": "https://www.linkedin.com/in/jane-doe-123", "title": "", "snippet": ""}]
    assert find_linkedin_company_url(results) == ""


def test_find_linkedin_company_url_none():
    assert find_linkedin_company_url([]) == ""
    assert find_linkedin_company_url([{"url": "https://example.com", "title": "", "snippet": ""}]) == ""


def test_find_linkedin_profile_url_matches_profiles():
    results = [
        {"url": "https://example.com/team", "title": "", "snippet": ""},
        {"url": "https://au.linkedin.com/in/jane-doe-123", "title": "Jane", "snippet": ""},
    ]
    assert find_linkedin_profile_url(results) == "https://au.linkedin.com/in/jane-doe-123"


def test_find_linkedin_profile_url_ignores_company_pages():
    results = [{"url": "https://www.linkedin.com/company/acme", "title": "", "snippet": ""}]
    assert find_linkedin_profile_url(results) == ""


# ---------------------------------------------------------------------------
# linkedin_block
# ---------------------------------------------------------------------------

def _extract(text):
    return lambda url: text


def test_linkedin_block_labels_the_page():
    url = "https://www.linkedin.com/company/acme"
    block = linkedin_block(url, _extract("Acme Construction - 500 followers"), max_chars=4000)
    assert f"[PAGE: LinkedIn — {url}]" in block
    assert "Acme Construction" in block


def test_linkedin_block_empty_extract_returns_empty():
    # A login-walled or unreadable page contributes nothing — never fabricated.
    assert linkedin_block("https://www.linkedin.com/company/acme", _extract("")) == ""


def test_linkedin_block_exception_returns_empty():
    def boom(url):
        raise RuntimeError("provider down")

    assert linkedin_block("https://www.linkedin.com/company/acme", boom) == ""


def test_linkedin_block_empty_url_returns_empty():
    assert linkedin_block("", _extract("content")) == ""


# ---------------------------------------------------------------------------
# linkedin_lane_enabled
# ---------------------------------------------------------------------------

def test_linkedin_lane_enabled_by_default(monkeypatch):
    monkeypatch.delenv("LINKEDIN_LANE_ENABLED", raising=False)
    assert linkedin_lane_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off"])
def test_linkedin_lane_disabled_values(monkeypatch, value):
    monkeypatch.setenv("LINKEDIN_LANE_ENABLED", value)
    assert linkedin_lane_enabled() is False


def test_linkedin_lane_enabled_true_value(monkeypatch):
    monkeypatch.setenv("LINKEDIN_LANE_ENABLED", "1")
    assert linkedin_lane_enabled() is True


# ---------------------------------------------------------------------------
# guard_evidence_location — exact-page teeth
# ---------------------------------------------------------------------------

def test_bare_root_verified_demoted():
    facts = [
        AIEvidence(claim="Full service contractor", source_url="https://acme.com", source_type="website", confidence="verified"),
    ]
    out = guard_evidence_location(facts)
    assert out[0].confidence == "unverified"
    # The honest reason is recorded; the claim + URL survive (unverified, not deleted).
    assert out[0].source_note == "source location not reported"
    assert out[0].source_url == "https://acme.com"


def test_bare_root_with_scheme_variants_demoted():
    for url in ("https://acme.com", "https://acme.com/", "acme.com", "http://acme.com"):
        out = guard_evidence_location([
            AIEvidence(claim="x", source_url=url, source_type="search_result", confidence="verified"),
        ])
        assert out[0].confidence == "unverified", url


def test_homepage_source_type_keeps_verified():
    # A claim explicitly citing the HOMEPAGE is not a "bare root with no
    # location" — the location IS the homepage.
    out = guard_evidence_location([
        AIEvidence(claim="Homepage tagline", source_url="https://acme.com", source_type="homepage", confidence="verified"),
    ])
    assert out[0].confidence == "verified"


def test_exact_page_verified_kept():
    out = guard_evidence_location([
        AIEvidence(claim="Founded 1990", source_url="https://acme.com/about", source_type="about_page", confidence="verified"),
        AIEvidence(claim="Team member", source_url="https://acme.com/team", source_type="team_page", confidence="verified"),
    ])
    assert [f.confidence for f in out] == ["verified", "verified"]


def test_unverified_and_empty_source_pass_through():
    facts = [
        AIEvidence(claim="guess", source_url="https://acme.com", source_type="inferred", confidence="unverified"),
        AIEvidence(claim="no source", source_url="", source_type="", confidence="unverified"),
    ]
    out = guard_evidence_location(facts)
    assert [f.confidence for f in out] == ["unverified", "unverified"]
    assert out[0].source_note == ""  # untouched unverified stays bare


def test_empty_source_verified_demoted():
    out = guard_evidence_location([
        AIEvidence(claim="claimed without source", source_url="", confidence="verified"),
    ])
    assert out[0].confidence == "unverified"


def test_existing_source_note_preserved_on_demotion():
    out = guard_evidence_location([
        AIEvidence(claim="x", source_url="https://acme.com", confidence="verified", source_note="team page"),
    ])
    assert out[0].confidence == "unverified"
    assert out[0].source_note == "team page"  # a real "where" survives untouched