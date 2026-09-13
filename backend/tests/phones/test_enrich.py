"""Enrichment engine contracts — hermetic (search + fetch + inference faked).

The engine's job is one honest mapping: phone lead -> the company's OWN
website -> emails literally on it; and when the crawl finds none, the P5
pattern-inference stage (faked here — the real one talks SMTP). These tests
pin the honesty rules (aggregator results are listings, not the company;
junk addresses are dropped; a miss is '') and the reuse seams (§14 —
parser, fetch, search).
"""

from __future__ import annotations

from app.phones.enrich import enrich_lead


class _Page:
    """FetchResult stand-in: just the two fields enrich_lead consumes."""

    def __init__(self, text: str = "", ok: bool = True):
        self.ok = ok
        self.text = text


def _no_infer(person, site):
    """Hermetic stand-in for the P5 engine: never verifies anything."""
    return {"email": "", "reason": "no_mx"}


def _lead(business="Acme GC", **kw):
    return {
        "business_name": business, "person_name": "Jane Smith",
        "city": "VANCOUVER", "state": "WA", "trade": "gc",
        **kw,
    }


def test_finds_email_on_homepage():
    calls = {"search": 0}

    def search(query, num):
        calls["search"] += 1
        assert "Acme GC" in query and "Vancouver" in query and "WA" in query
        return ["https://acmegc.com"]

    def fetch(url, **kw):
        return _Page("<p>Email info@acmegc.com or call today</p>")

    out = enrich_lead(_lead(), search_fn=search, fetch_fn=fetch,
                      infer_fn=_no_infer)
    assert out == {"email": "info@acmegc.com", "email_source": "website",
                   "website": "https://acmegc.com",
                   "dork": "phone_enrichment"}
    assert calls["search"] == 1  # one search, not a loop


def test_follows_contact_page_when_homepage_has_no_email():
    fetched = []

    def fetch(url, **kw):
        fetched.append(url)
        if url == "https://acmegc.com":
            return _Page(
                '<a href="https://acmegc.com/contact-us">Contact</a>'
            )
        return _Page("<p>Reach us at sales@acmegc.com</p>")

    out = enrich_lead(
        _lead(), search_fn=lambda q, n: ["https://acmegc.com"], fetch_fn=fetch,
    )
    assert out["email"] == "sales@acmegc.com"
    assert "https://acmegc.com/contact-us" in fetched


def test_prefers_own_domain_over_free_mail():
    def fetch(url, **kw):
        return _Page(
            "<p>Gmail: acmegc@gmail.com — official: info@acmegc.com</p>"
        )

    out = enrich_lead(
        _lead(), search_fn=lambda q, n: ["https://acmegc.com"], fetch_fn=fetch,
    )
    assert out["email"] == "info@acmegc.com"


def test_aggregator_results_are_not_the_company():
    """Yelp/Facebook pages about the business are listings — never crawled."""

    def fetch(url, **kw):
        if "yelp" in url or "facebook" in url:
            raise AssertionError(f"should not crawl a listing: {url}")
        return _Page("<p>No email here</p>")

    out = enrich_lead(
        _lead(),
        search_fn=lambda q, n: [
            "https://www.yelp.com/biz/acme", "https://facebook.com/acme",
            "https://acmegc.com",
        ],
        fetch_fn=fetch, infer_fn=_no_infer,
    )
    assert out["website"] == "https://acmegc.com"
    assert out["email"] == ""  # honest miss on the real site


def test_no_website_is_an_honest_miss():
    out = enrich_lead(
        _lead(), search_fn=lambda q, n: [], fetch_fn=lambda u, **k: _Page(),
        infer_fn=_no_infer,
    )
    assert out == {"email": "", "email_source": "", "website": "",
                   "dork": ""}


def test_no_email_on_site_is_an_honest_miss():
    """Website reachable but email-less: the SITE is still recorded (it is
    the provenance of the attempt and P5's pattern-inference input)."""
    out = enrich_lead(
        _lead(),
        search_fn=lambda q, n: ["https://acmegc.com"],
        fetch_fn=lambda u, **k: _Page("<p>We do great work</p>"),
        infer_fn=_no_infer,
    )
    assert out["email"] == "" and out["email_source"] == ""
    assert out["website"] == "https://acmegc.com"


def test_junk_emails_are_dropped():
    def fetch(url, **kw):
        return _Page(
            "<p>logo@acmegc.com.png example@example.com "
            "info@acmegc.com noreply@acmegc.com</p>"
        )

    out = enrich_lead(
        _lead(), search_fn=lambda q, n: ["https://acmegc.com"], fetch_fn=fetch,
    )
    assert out["email"] == "info@acmegc.com"


def test_free_mail_on_own_site_is_kept_as_fallback():
    """A gmail on the company's own contact page is REAL evidence for a
    calling user — kept (the emails-vertical feed applies its own stricter
    free-mail rule at ITS intake, not here)."""

    def fetch(url, **kw):
        return _Page("<p>Email bobsgc@gmail.com</p>")

    out = enrich_lead(
        _lead(business="Bobs GC"),
        search_fn=lambda q, n: ["https://bobsgc.com"], fetch_fn=fetch,
    )
    assert out["email"] == "bobsgc@gmail.com"


def test_no_business_name_no_search():
    calls = {"n": 0}

    def search(q, n):
        calls["n"] += 1
        return ["https://x.com"]

    out = enrich_lead(_lead(business=""), search_fn=search,
                      fetch_fn=lambda u, **k: _Page(), infer_fn=_no_infer)
    assert out == {"email": "", "email_source": "", "website": "",
                   "dork": ""}
    assert calls["n"] == 0  # nothing to search for — no wasted query


# -- stage 2: the P5 pattern-inference fallback --------------------------------


def test_inference_fills_the_miss_with_a_verified_permutation():
    """Crawl finds nothing -> the mail-server-confirmed permutation wins,
    tagged so the emails-vertical feed knows which lane produced it."""

    def infer(person, site):
        assert person == "Jane Smith" and site == "https://acmegc.com"
        return {"email": "jane.smith@acmegc.com", "reason": "verified"}

    out = enrich_lead(
        _lead(),
        search_fn=lambda q, n: ["https://acmegc.com"],
        fetch_fn=lambda u, **k: _Page("<p>We do great work</p>"),
        infer_fn=infer,
    )
    assert out == {
        "email": "jane.smith@acmegc.com",
        "email_source": "pattern_inference",
        "website": "https://acmegc.com",
        "dork": "pattern_inference",
    }


def test_inference_miss_is_still_an_honest_miss():
    """Catch-all / greylist / no-MX from the engine: '' with the site kept."""

    def infer(person, site):
        return {"email": "", "reason": "catch_all"}

    out = enrich_lead(
        _lead(),
        search_fn=lambda q, n: ["https://acmegc.com"],
        fetch_fn=lambda u, **k: _Page("<p>We do great work</p>"),
        infer_fn=infer,
    )
    assert out == {"email": "", "email_source": "",
                   "website": "https://acmegc.com", "dork": ""}


def test_no_person_name_skips_inference():
    """Nothing to permutate without a person — no inference call at all."""
    calls = {"n": 0}

    def infer(person, site):
        calls["n"] += 1
        return {"email": "x@y.z", "reason": "verified"}

    out = enrich_lead(
        _lead(person_name=""),
        search_fn=lambda q, n: ["https://acmegc.com"],
        fetch_fn=lambda u, **k: _Page("<p>We do great work</p>"),
        infer_fn=infer,
    )
    assert out["email"] == "" and out["website"] == "https://acmegc.com"
    assert calls["n"] == 0


def test_crawl_email_beats_inference():
    """An address literally SEEN on the site is stronger evidence than a
    server-confirmed permutation — the crawl stage's answer stands."""

    def infer(person, site):
        return {"email": "jane.smith@acmegc.com", "reason": "verified"}

    out = enrich_lead(
        _lead(),
        search_fn=lambda q, n: ["https://acmegc.com"],
        fetch_fn=lambda u, **k: _Page("<p>Email info@acmegc.com</p>"),
        infer_fn=infer,
    )
    assert out["email"] == "info@acmegc.com"
    assert out["email_source"] == "website"
