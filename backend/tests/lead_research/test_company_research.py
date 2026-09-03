"""CompanyResearcher — deterministic tests with injected seams."""

from __future__ import annotations

import json

from app.lead_research.company_research import (
    CompanyResearcher,
    _parse_ai_json,
    _truncate_html,
    default_refine_domain,
)
from tests.lead_research.conftest import (
    make_fake_ai,
    make_fake_fetch,
    make_fake_refine,
    make_fake_search,
)


# ---------------------------------------------------------------------------
# _parse_ai_json
# ---------------------------------------------------------------------------

def test_parse_json_clean():
    assert _parse_ai_json('{"key": "value"}') == {"key": "value"}


def test_parse_json_with_fences():
    raw = '```json\n{"key": "value"}\n```'
    assert _parse_ai_json(raw) == {"key": "value"}


def test_parse_json_with_fences_no_lang():
    raw = '```\n{"key": "value"}\n```'
    assert _parse_ai_json(raw) == {"key": "value"}


def test_parse_json_garbage_returns_empty():
    assert _parse_ai_json("this is not json at all") == {}


# ---------------------------------------------------------------------------
# _truncate_html
# ---------------------------------------------------------------------------

def test_truncate_html_strips_tags():
    html = "<html><body>Hello <b>world</b></body></html>"
    result = _truncate_html(html)
    assert "<" not in result
    assert "Hello" in result
    assert "world" in result


def test_truncate_html_removes_script():
    html = "<script>evil();</script><body>safe</body>"
    result = _truncate_html(html)
    assert "evil" not in result
    assert "safe" in result


def test_truncate_html_limits_length():
    html = "<p>" + "x" * 10000 + "</p>"
    result = _truncate_html(html, max_chars=500)
    assert len(result) <= 500


# ---------------------------------------------------------------------------
# default_refine_domain
# ---------------------------------------------------------------------------

def test_refine_domain_already_valid():
    """Domain with MX → keep as-is."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"

    result = default_refine_domain("acme.com", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_fixes_typo():
    """Typo TLD (.comz) → corrected to .com."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"  # .comz won't match, .com will

    result = default_refine_domain("acme.comz", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_strips_www():
    """www.acme.com → acme.com."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"

    result = default_refine_domain("www.acme.com", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_strips_scheme():
    """https://acme.com/path → acme.com."""
    def mx_ok(d: str) -> bool:
        return d == "acme.com"

    result = default_refine_domain("https://acme.com/some/path", mx_check=mx_ok)
    assert result == "acme.com"


def test_refine_domain_no_mx_returns_normalized():
    """No MX resolves → return normalized original."""
    result = default_refine_domain("acme.xyz", mx_check=lambda d: False)
    assert result == "acme.xyz"


def test_refine_domain_empty_returns_original():
    result = default_refine_domain("", mx_check=lambda d: False)
    assert result == ""


# ---------------------------------------------------------------------------
# CompanyResearcher.research
# ---------------------------------------------------------------------------

def _good_ai_response():
    return {
        "company_name": "Acme Construction",
        "industry": "General Contractor",
        "location": "Dallas, TX",
        "website": "https://acme.com",
        "facts": [
            {"claim": "Founded in 1990", "source_url": "https://acme.com/about", "source_type": "website", "confidence": "verified"},
            {"claim": "Revenue ~$10M", "source_url": "", "source_type": "inferred", "confidence": "unverified"},
        ],
    }


def test_research_happy_path():
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == "Acme Construction"
    assert profile.industry == "General Contractor"
    assert profile.location == "Dallas, TX"
    assert len(profile.facts) == 2
    assert profile.facts[0].confidence == "verified"
    assert profile.facts[1].confidence == "unverified"


def test_research_ai_returns_empty_json():
    """AI returns empty JSON {} → treated as no data, partial profile with error fact."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai({}),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == ""
    # Empty dict {} is valid JSON but means no useful data → unparseable error
    assert len(profile.facts) == 1
    assert "unparseable" in profile.facts[0].claim.lower()


def test_research_ai_returns_garbage():
    """AI returns non-JSON → partial profile, no crash."""
    researcher = CompanyResearcher(
        ai_ask=lambda prompt: "this is not json",
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == ""
    assert len(profile.facts) == 1
    assert "unparseable" in profile.facts[0].claim.lower()


def test_research_ai_raises_exception():
    """AI call throws → partial profile with error fact."""
    def boom(prompt: str) -> str:
        raise RuntimeError("API down")

    researcher = CompanyResearcher(
        ai_ask=boom,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == ""
    assert "failed" in profile.facts[0].claim.lower()


def test_research_includes_refined_domain_in_prompt():
    """Verify refined domain (not original) is used in the prompt."""
    captured = {}

    def capturing_ai(prompt: str) -> str:
        captured["prompt"] = prompt
        return json.dumps(_good_ai_response())

    researcher = CompanyResearcher(
        ai_ask=capturing_ai,
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),  # refines from acme.comz
    )
    researcher.research("john@acme.comz", "acme.comz")
    assert "acme.com" in captured["prompt"]
    assert "john@acme.comz" in captured["prompt"]


def test_research_gathers_multiple_search_queries():
    """Multiple search queries are run and deduplicated."""
    seen = []

    def tracking_search(query: str) -> list[dict[str, str]]:
        seen.append(query)
        return []

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=tracking_search,
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    researcher.research("john@acme.com", "acme.com")
    assert len(seen) == 3  # email, domain, domain company


def test_research_fetches_home_about_contact():
    """Fetches homepage, /about, and /contact pages."""
    fetched_urls = []

    def tracking_fetch(url: str):
        fetched_urls.append(url)
        mock = type("M", (), {"ok": True, "html": "<html>page</html>"})()
        return mock

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=tracking_fetch,
        refine_domain=make_fake_refine("acme.com"),
    )
    researcher.research("john@acme.com", "acme.com")
    assert "https://acme.com" in fetched_urls
    assert "https://acme.com/about" in fetched_urls
    assert "https://acme.com/contact" in fetched_urls


def test_research_fetch_failure_doesnt_crash():
    """Fetch failures are silently skipped."""
    def fail_fetch(url: str):
        raise ConnectionError("timeout")

    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=fail_fetch,
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == "Acme Construction"


def test_research_empty_search_results():
    """Empty search results → still works."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search([]),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    profile = researcher.research("john@acme.com", "acme.com")
    assert profile.name == "Acme Construction"


def test_research_with_domain_returns_dict():
    """research_with_domain returns dict including refined_domain."""
    researcher = CompanyResearcher(
        ai_ask=make_fake_ai(_good_ai_response()),
        search=make_fake_search(),
        fetch_page=make_fake_fetch(),
        refine_domain=make_fake_refine("acme.com"),
    )
    result = researcher.research_with_domain("john@acme.comz", "acme.comz")
    assert result["refined_domain"] == "acme.com"
    assert result["original_domain"] == "acme.comz"
    assert result["name"] == "Acme Construction"
