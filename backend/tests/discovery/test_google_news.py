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
