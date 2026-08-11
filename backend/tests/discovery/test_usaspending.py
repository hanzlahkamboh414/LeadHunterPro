"""Offline tests for the usaspending intent plugin (Increment 5).

The award-search API is served through a monkeypatched ``requests`` — no
live network (CLAUDE.md §1). Each test pins one honest outcome: awards
become traceable ``bid_award`` evidence on their public USAspending
page; an empty result is ``EMPTY``; an unreachable API is ``UNAVAILABLE``.
"""

from __future__ import annotations

import types

import requests

from app.discovery.intent.usaspending import USAspendingPlugin
from app.discovery.sources.status import SourceStatus
from app.engines.lead.lead_models import IntentEvidenceType

AWARDS_JSON = {
    "results": [
        {
            "Award ID": "ABC123",
            "Recipient Name": "TEXAS SKYLINE ROOFING LLC",
            "Award Amount": 500000.0,
            "Awarding Agency": "General Services Administration",
            "Start Date": "2026-06-01",
            "End Date": "2027-06-01",
            "Description": "Roofing services",
        },
        {
            "Award ID": "DEF456",
            "Recipient Name": "TEXAS SKYLINE ROOFING LLC",
            "Award Amount": 75000,
            "Awarding Agency": "Department of Defense",
            "Start Date": "",
            "End Date": "2026-09-30",
            "Description": "",
        },
    ]
}

EMPTY_JSON = {"results": []}


class _FakeRequests:
    def __init__(self, payload=None, status=200, error=None):
        self.payload = payload
        self.status = status
        self.error = error
        self.last_post = None

    def post(self, url, json=None, timeout=30):
        self.last_post = json
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(status_code=self.status, json=lambda: self.payload)


class TestUSAspendingPlugin:

    def _plugin(self, monkeypatch, fake):
        monkeypatch.setattr("app.discovery.intent.usaspending.requests", fake)
        return USAspendingPlugin()

    def test_awards_become_traceable_bid_evidence(self, monkeypatch):
        fake = _FakeRequests(payload=AWARDS_JSON)
        plugin = self._plugin(monkeypatch, fake)
        status, evidence, _ = plugin.collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )
        assert status is SourceStatus.SUCCESS
        assert len(evidence) == 2

        first = evidence[0]
        assert first.type is IntentEvidenceType.bid_award
        assert first.source == "usaspending"
        assert first.source_url == "https://www.usaspending.gov/award/ABC123"
        assert first.date == "2026-06-01"
        assert "TEXAS SKYLINE ROOFING LLC" in first.snippet
        assert "$500,000" in first.snippet

        second = evidence[1]
        assert second.source_url == "https://www.usaspending.gov/award/DEF456"
        assert second.date == "2026-09-30"  # falls back to End Date when no Start Date

    def test_request_targets_the_recipient(self, monkeypatch):
        fake = _FakeRequests(payload=AWARDS_JSON)
        plugin = self._plugin(monkeypatch, fake)
        plugin.collect_evidence(company_name="TEXAS SKYLINE ROOFING LLC")
        filters = fake.last_post["filters"]
        assert filters["recipient_search_text"] == ["TEXAS SKYLINE ROOFING LLC"]

    def test_no_awards_is_empty(self, monkeypatch):
        plugin = self._plugin(monkeypatch, _FakeRequests(payload=EMPTY_JSON))
        status, evidence, _ = plugin.collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )
        assert status is SourceStatus.EMPTY
        assert evidence == []

    def test_award_without_id_is_not_evidence(self, monkeypatch):
        plugin = self._plugin(
            monkeypatch,
            _FakeRequests(payload={"results": [{"Recipient Name": "X"}]}),
        )
        status, evidence, _ = plugin.collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )
        # no Award ID -> no traceable source_url -> dropped honestly
        assert status is SourceStatus.EMPTY
        assert evidence == []

    def test_non_200_is_unavailable(self, monkeypatch):
        plugin = self._plugin(monkeypatch, _FakeRequests(payload=EMPTY_JSON, status=500))
        status, evidence, _ = plugin.collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )
        assert status is SourceStatus.UNAVAILABLE
        assert evidence == []

    def test_network_error_is_unavailable(self, monkeypatch):
        fake = _FakeRequests(error=requests.ConnectionError("down"))
        plugin = self._plugin(monkeypatch, fake)
        status, evidence, _ = plugin.collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )
        assert status is SourceStatus.UNAVAILABLE
        assert evidence == []
