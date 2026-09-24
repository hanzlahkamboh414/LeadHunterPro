"""Offline tests for the google_news intent plugin (Increment 5).

The RSS feed is served through a monkeypatched ``requests`` — no live
network (CLAUDE.md §1). Each test pins one honest outcome: feed items
become traceable news evidence; an empty feed is ``EMPTY``; an
unreachable feed is ``UNAVAILABLE`` — never a fabricated headline.
"""

from __future__ import annotations

import types

import requests

from app.discovery.intent.google_news import GoogleNewsPlugin
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidenceType

NEWS_RSS_TWO_ITEMS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Google News</title>
<item>
  <title>Texas Skyline Roofing expands to San Antonio</title>
  <link>https://news.google.com/rss/articles/AAA</link>
  <pubDate>Mon, 03 Aug 2026 14:00:00 GMT</pubDate>
  <description>Local roofing firm opens a new branch.</description>
</item>
<item>
  <title>Texas Skyline Roofing wins big Dallas contract</title>
  <link>https://news.google.com/rss/articles/BBB</link>
  <pubDate>Fri, 07 Aug 2026 09:30:00 GMT</pubDate>
  <description>Contract award details.</description>
</item>
</channel>
</rss>
"""

EMPTY_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Google News</title></channel></rss>
"""


class _FakeRequests:
    def __init__(self, text="", status=200, error=None):
        self.text = text
        self.status = status
        self.error = error
        self.last_params = None

    def get(self, url, params=None, timeout=15):
        self.last_params = params
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(status_code=self.status, text=self.text)


class TestGoogleNewsPlugin:

    def _plugin(self, monkeypatch, fake):
        monkeypatch.setattr("app.discovery.intent.google_news.requests", fake)
        return GoogleNewsPlugin()

    def test_items_become_traceable_news_evidence(self, monkeypatch):
        fake = _FakeRequests(text=NEWS_RSS_TWO_ITEMS)
        plugin = self._plugin(monkeypatch, fake)
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing"
        )
        assert status is SourceStatus.SUCCESS
        assert len(evidence) == 2
        assert all(e.type is IntentEvidenceType.news for e in evidence)
        assert all(e.source == "google_news" for e in evidence)
        assert evidence[0].source_url == "https://news.google.com/rss/articles/AAA"
        assert evidence[0].snippet == "Texas Skyline Roofing expands to San Antonio"
        assert evidence[0].date == "Mon, 03 Aug 2026 14:00:00 GMT"
        # the query carries the company name
        assert fake.last_params["q"] == "Texas Skyline Roofing"

    def test_location_is_appended_to_query(self, monkeypatch):
        fake = _FakeRequests(text=NEWS_RSS_TWO_ITEMS)
        plugin = self._plugin(monkeypatch, fake)
        plugin.collect_evidence(
            company_name="Texas Skyline Roofing",
            location="Dallas TX",
        )
        assert fake.last_params["q"] == "Texas Skyline Roofing Dallas TX"

    def test_empty_feed_is_empty(self, monkeypatch):
        plugin = self._plugin(monkeypatch, _FakeRequests(text=EMPTY_FEED))
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing"
        )
        assert status is SourceStatus.EMPTY
        assert evidence == []

    def test_non_200_is_unavailable(self, monkeypatch):
        plugin = self._plugin(monkeypatch, _FakeRequests(text=EMPTY_FEED, status=503))
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing"
        )
        assert status is SourceStatus.UNAVAILABLE
        assert evidence == []

    def test_network_error_is_unavailable(self, monkeypatch):
        fake = _FakeRequests(error=requests.ConnectionError("down"))
        plugin = self._plugin(monkeypatch, fake)
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing"
        )
        assert status is SourceStatus.UNAVAILABLE
        assert evidence == []

    def test_malformed_xml_is_empty(self, monkeypatch):
        plugin = self._plugin(monkeypatch, _FakeRequests(text="<rss><broken"))
        status, evidence, _ = plugin.collect_evidence(
            company_name="Texas Skyline Roofing"
        )
        assert status is SourceStatus.EMPTY
        assert evidence == []


#: A feed whose items include the live failure measured against the stored
#: Turner rows: Google News matches a quoted name loosely, so it returns
#: articles that mention the company in the body while the headline names a
#: different firm.
NEWS_RSS_MIXED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Google News</title>
<item>
  <title>Turner Construction wins Dallas contract</title>
  <link>https://news.google.com/rss/articles/ONE</link>
  <pubDate>Mon, 03 Aug 2026 14:00:00 GMT</pubDate>
</item>
<item>
  <title>Jacobs wins role on $1.7B New York public health lab</title>
  <link>https://news.google.com/rss/articles/TWO</link>
  <pubDate>Fri, 07 Aug 2026 09:30:00 GMT</pubDate>
</item>
<item>
  <title>Construction worker dies at site of Broncos new training facility</title>
  <link>https://news.google.com/rss/articles/THREE</link>
  <pubDate>Sat, 08 Aug 2026 09:30:00 GMT</pubDate>
</item>
<item>
  <title>Turner discloses data breach of salary info</title>
  <link>https://news.google.com/rss/articles/FOUR</link>
  <pubDate>Sun, 09 Aug 2026 09:30:00 GMT</pubDate>
</item>
</channel>
</rss>
"""

#: Every item is off-company — the honest outcome is EMPTY, not SUCCESS.
NEWS_RSS_ALL_OFF_COMPANY = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>Google News</title>
<item>
  <title>ENR 2026 Top 400 Contractors 1-100</title>
  <link>https://news.google.com/rss/articles/ENR</link>
</item>
</channel>
</rss>
"""


class TestGoogleNewsCompanyCheck:
    """Defect fixed 2026-09-21: every feed item became evidence unchecked.

    Measured live against the stored Turner rows: 42 of 198 (21%) did not
    name the company at all. The headline is the ONLY text the record keeps,
    so once it fails to name the company there is nothing left in the record
    that could ever tie it back — and ``company_match`` consequently reads
    ``unknown`` and the pain gate refuses it.
    """

    def _plugin(self, monkeypatch, fake):
        monkeypatch.setattr("app.discovery.intent.google_news.requests", fake)
        return GoogleNewsPlugin()

    def test_items_that_do_not_name_the_company_are_dropped(self, monkeypatch):
        fake = _FakeRequests(text=NEWS_RSS_MIXED)
        plugin = self._plugin(monkeypatch, fake)
        status, evidence, _ = plugin.collect_evidence(
            company_name="Turner Construction"
        )
        assert status is SourceStatus.SUCCESS
        assert [e.source_url for e in evidence] == [
            "https://news.google.com/rss/articles/ONE",
            "https://news.google.com/rss/articles/FOUR",
        ]

    def test_the_drop_is_counted_not_silent(self, monkeypatch):
        """A drop nobody can see is indistinguishable from a thin source."""
        fake = _FakeRequests(text=NEWS_RSS_MIXED)
        plugin = self._plugin(monkeypatch, fake)
        _, _, meta = plugin.collect_evidence(company_name="Turner Construction")
        assert meta["results"] == 2
        assert meta["items_off_company"] == 2

    def test_a_generic_trade_word_is_not_a_name_match(self, monkeypatch):
        """"Construction" is in the company name and in every headline.

        Without the generic-trade guard the Broncos headline passes on the
        word alone, which is precisely how off-company rows got stored.
        """
        fake = _FakeRequests(text=NEWS_RSS_MIXED)
        plugin = self._plugin(monkeypatch, fake)
        _, evidence, _ = plugin.collect_evidence(company_name="Turner Construction")
        assert not any("Broncos" in e.snippet for e in evidence)

    def test_a_feed_that_is_entirely_off_company_is_empty_not_success(
        self, monkeypatch
    ):
        fake = _FakeRequests(text=NEWS_RSS_ALL_OFF_COMPANY)
        plugin = self._plugin(monkeypatch, fake)
        status, evidence, meta = plugin.collect_evidence(
            company_name="Turner Construction"
        )
        assert status is SourceStatus.EMPTY
        assert evidence == []
        assert meta["items_off_company"] == 1
        assert meta["note"] == "returned items, none naming the company"

    def test_the_check_uses_the_shared_rule_not_a_second_approximation(
        self, monkeypatch
    ):
        """The plugin and the ``company_match`` classifier must agree.

        They agree by construction — both call
        ``identity_verifier.name_on_page`` — and this pins that, because two
        answers to "is the company named here?" is the drift that produced
        the split-brain email validators (CLAUDE.md §14).
        """
        from app.engines.verification.identity_verifier import (
            name_on_page,
            name_tokens,
        )
        from app.research.adapters import classify_company_match

        fake = _FakeRequests(text=NEWS_RSS_MIXED)
        plugin = self._plugin(monkeypatch, fake)
        _, evidence, _ = plugin.collect_evidence(company_name="Turner Construction")

        for item in evidence:
            assert name_on_page(
                "Turner Construction", name_tokens("Turner Construction"),
                item.snippet,
            )
            assert classify_company_match(
                company_name="Turner Construction",
                domain="turnerconstruction.com",
                source_url=item.source_url,
                text=item.snippet,
            ).value == "confirmed"
