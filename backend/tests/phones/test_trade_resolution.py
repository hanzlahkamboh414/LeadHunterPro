"""Trade qualification requires the business's own page, not a name guess."""

from __future__ import annotations

from app.phones.enrich import resolve_trade


class _Page:
    def __init__(self, html: str, ok: bool = True):
        self.text = html
        self.ok = ok


def _lead(**changes):
    return {
        "phone": "+16125550100", "business_name": "North Star Roofing LLC",
        "city": "MINNEAPOLIS", "state": "MN", "person_name": "",
        **changes,
    }


def _verified_page():
    return ("<html><title>North Star Roofing | Roofing Contractor</title>"
            '<meta name="description" content="Residential roofing contractor">'
            "<body><h1>North Star Roofing</h1>"
            "<p>Call (612) 555-0100 for roof repair.</p></body></html>")


def test_own_page_with_matching_identity_and_trade_is_accepted():
    out = resolve_trade(
        _lead(), search_fn=lambda q, n: ["https://northstarroofing.com/services"],
        fetch_fn=lambda u, **kw: _Page(_verified_page()),
    )
    assert out == {
        "trade": "roofing",
        "evidence_url": "https://northstarroofing.com/services",
        "evidence_kind": "company_website",
    }


def test_matching_trade_without_matching_phone_is_rejected():
    html = _verified_page().replace("(612) 555-0100", "(612) 555-0199")
    out = resolve_trade(
        _lead(), search_fn=lambda q, n: ["https://northstarroofing.com"],
        fetch_fn=lambda u, **kw: _Page(html),
    )
    assert out["trade"] == ""


def test_matching_phone_without_business_identity_is_rejected():
    html = _verified_page().replace("North Star Roofing", "Other Roofing")
    out = resolve_trade(
        _lead(), search_fn=lambda q, n: ["https://otherroofing.com"],
        fetch_fn=lambda u, **kw: _Page(html),
    )
    assert out["trade"] == ""


def test_board_name_alone_never_supplies_trade():
    out = resolve_trade(
        _lead(), search_fn=lambda q, n: ["https://northstarroofing.com"],
        fetch_fn=lambda u, **kw: _Page(
            "<title>North Star Roofing</title><p>Call (612) 555-0100</p>"),
    )
    assert out["trade"] == ""


def test_listing_is_never_used_as_trade_evidence():
    out = resolve_trade(
        _lead(), search_fn=lambda q, n: ["https://yelp.com/biz/north-star"],
        fetch_fn=lambda u, **kw: (_ for _ in ()).throw(
            AssertionError("directory must not be crawled")),
    )
    assert out["trade"] == ""
