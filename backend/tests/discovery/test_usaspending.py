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
    def __init__(self, payload=None, status=200, error=None, text="", json_error=None):
        self.payload = payload
        self.status = status
        self.error = error
        self.text = text
        self.json_error = json_error
        self.last_post = None

    def post(self, url, json=None, timeout=30):
        self.last_post = json
        if self.error is not None:
            raise self.error

        def _json():
            if self.json_error is not None:
                raise self.json_error
            return self.payload

        return types.SimpleNamespace(
            status_code=self.status, text=self.text, json=_json
        )


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

    def test_non_200_response_is_an_error(self, monkeypatch):
        plugin = self._plugin(monkeypatch, _FakeRequests(payload=EMPTY_JSON, status=500))
        status, evidence, _ = plugin.collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )
        assert status is SourceStatus.ERROR
        assert evidence == []

    def test_network_error_is_unavailable(self, monkeypatch):
        fake = _FakeRequests(error=requests.ConnectionError("down"))
        plugin = self._plugin(monkeypatch, fake)
        status, evidence, metadata = plugin.collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )
        assert status is SourceStatus.UNAVAILABLE
        assert evidence == []
        assert metadata["reason"] == "access_error"


class TestRequiredRequestFields:
    """The defect of 2026-09-18, pinned.

    This plugin shipped without ``filters.award_type_codes`` and without
    ``subawards``. Both are required by the endpoint, so every real call
    answered ``422`` and the plugin had never returned a single award — yet
    it reported ``UNAVAILABLE``, which reads as a passing outage. Verified
    live against the API: as shipped -> 422 with the body "Missing value:
    'filters|award_type_codes' is a required field"; with both keys -> 200
    and real awards. Offline, these tests are what stop that regressing.
    """

    def test_the_required_award_type_codes_are_sent(self, monkeypatch):
        fake = _FakeRequests(payload=AWARDS_JSON)
        plugin = USAspendingPlugin()
        monkeypatch.setattr("app.discovery.intent.usaspending.requests", fake)
        plugin.collect_evidence(company_name="TEXAS SKYLINE ROOFING LLC")

        codes = fake.last_post["filters"]["award_type_codes"]
        assert codes, "omitting this is a 422, not an empty search"
        assert all(isinstance(code, str) and code for code in codes)

    def test_the_required_subawards_flag_is_sent(self, monkeypatch):
        fake = _FakeRequests(payload=AWARDS_JSON)
        plugin = USAspendingPlugin()
        monkeypatch.setattr("app.discovery.intent.usaspending.requests", fake)
        plugin.collect_evidence(company_name="TEXAS SKYLINE ROOFING LLC")

        assert fake.last_post["subawards"] is False, (
            "required by the endpoint; absent means 422"
        )


class TestFailureAttribution:
    """Status says whether the source answered; reason says who is broken.

    A 4xx is OUR bug (a malformed request), a 5xx/unparseable response is a
    source error, and a timeout means it never answered. Filing all three as
    unavailable is how a dead request body read as a flaky endpoint for months.
    """

    def _plugin(self, monkeypatch, fake):
        monkeypatch.setattr("app.discovery.intent.usaspending.requests", fake)
        return USAspendingPlugin()

    def test_a_rejected_request_is_our_error_not_their_outage(self, monkeypatch):
        fake = _FakeRequests(
            status=422,
            text='{"detail":"Missing value: \'filters|award_type_codes\' is a required field"}',
        )
        status, evidence, metadata = self._plugin(monkeypatch, fake).collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )

        assert status is SourceStatus.ERROR
        assert status is not SourceStatus.UNAVAILABLE, (
            "a request we built wrong is not the source being down"
        )
        assert evidence == []
        assert metadata["reason"] == "request_error"
        assert metadata["status"] == 422
        assert "award_type_codes" in metadata["detail"], (
            "the API names the missing field; throwing that away leaves "
            "'unavailable' as the only clue"
        )

    def test_a_server_failure_is_theirs(self, monkeypatch):
        fake = _FakeRequests(status=503, text="Service Unavailable")
        status, _, metadata = self._plugin(monkeypatch, fake).collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )

        assert status is SourceStatus.ERROR
        assert metadata["reason"] == "source_error"

    def test_an_unparsable_body_is_theirs(self, monkeypatch):
        fake = _FakeRequests(payload=None, json_error=ValueError("not json"))
        status, _, metadata = self._plugin(monkeypatch, fake).collect_evidence(
            company_name="TEXAS SKYLINE ROOFING LLC"
        )

        assert status is SourceStatus.ERROR
        assert metadata["reason"] == "source_error"

    def test_the_three_failure_kinds_are_distinguishable(self, monkeypatch):
        """The whole point: one status vocabulary, three different stories.

        Only a transport failure is ``UNAVAILABLE`` because it is the only
        case where the source was never reached.
        """
        cases = [
            (_FakeRequests(status=400), SourceStatus.ERROR, "request_error"),
            (_FakeRequests(status=500), SourceStatus.ERROR, "source_error"),
            (
                _FakeRequests(error=requests.Timeout("slow")),
                SourceStatus.UNAVAILABLE,
                "access_error",
            ),
        ]
        seen = []
        for fake, expected_status, expected_reason in cases:
            status, _, metadata = self._plugin(monkeypatch, fake).collect_evidence(
                company_name="TEXAS SKYLINE ROOFING LLC"
            )
            assert status is expected_status
            assert metadata["reason"] == expected_reason
            seen.append(metadata["reason"])

        assert len(set(seen)) == 3, "three failures that read differently"
