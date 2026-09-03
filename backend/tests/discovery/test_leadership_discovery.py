"""Offline tests for the Increment-3 domain-tier wiring in LeadershipDiscovery.

The saved team-page fixture is served for every candidate URL and ``requests``
is monkeypatched for BOTH the page fetch and the MX lookup, so the whole
discover() path runs offline. Proves a generic mailbox bound to a person is
upgraded to ``domain`` when its domain resolves, while every ``person_bound``
address keeps its tier.

``TestScanDiagnostics`` covers roadmap D20: the same offline harness, used to
prove a fruitless scan now reports which of the three failure modes occurred.
"""

from __future__ import annotations

import logging
import types

import requests

from app.discovery.leadership_discovery import LeadershipDiscovery
from tests.fixtures.people_pages import TEAM_GRID_HTML


class _FakeRequests:
    """Serves the saved team page for page fetches and MX answers for lookups."""

    def get(self, *args, **kwargs):
        return types.SimpleNamespace(
            status_code=200,
            text=TEAM_GRID_HTML,
            json=lambda: {"Answer": [{"type": 15, "data": "10 mx.texasskylineco.com"}]},
        )


class TestDomainTierWiring:

    def _discover(self, monkeypatch):
        fake = _FakeRequests()
        monkeypatch.setattr("app.discovery.leadership_discovery.requests", fake)
        monkeypatch.setattr("app.email.domain_verifier.requests", fake)
        return LeadershipDiscovery().discover("https://texasskylineco.com")

    def test_generic_email_upgraded_to_domain(self, monkeypatch):
        leaders = self._discover(monkeypatch)
        tiers = {e["email"]: e["tier"] for r in leaders for e in r["emails"]}

        # info@ is bound to Maria as a generic mailbox -> domain when MX resolves
        assert tiers["info@texasskylineco.com"] == "domain"
        # personal mailboxes keep the higher person_bound tier
        assert tiers["m.gomez@texasskylineco.com"] == "person_bound"
        assert tiers["j.reed@texasskylineco.com"] == "person_bound"
        assert tiers["sue@texasskylineco.com"] == "person_bound"
        # the footer office@ is in no person region -> never bound, never verified
        assert "office@texasskylineco.com" not in tiers

    def test_every_bound_email_keeps_an_honest_tier(self, monkeypatch):
        leaders = self._discover(monkeypatch)
        tiers = {e["email"]: e["tier"] for r in leaders for e in r["emails"]}
        assert set(tiers.values()) <= {"domain", "person_bound", "format"}

    def test_same_person_across_pages_emitted_once(self, monkeypatch):
        """The same team page served for every candidate URL collapses to one
        record per (name, role) — not one per page (Inc11 evidence: Chris
        Arrington appeared 6× across candidate pages)."""
        leaders = self._discover(monkeypatch)
        names = {(r["person"]["name"], r["person"]["role"]) for r in leaders}
        assert names == {
            ("Maria Gomez", "President"),
            ("Jake Reed", "Project Manager"),
            # "Marketing" is a ROLE_MODIFIER, not a title keyword — the longest
            # detected role is "Director" (both flag role_relevance False).
            ("Sue Lee", "Director"),
        }


class _TwoPageFake:
    """President on the homepage with no email; /contact binds a mailto."""

    _HOME = "<div class='bio'><h3>Chris Arrington</h3><p>President</p></div>"
    _CONTACT = (
        "<div class='bio'><h3>Chris Arrington</h3><p>President</p>"
        "<a href='mailto:chris@arringtonroofing.com'>Email Chris</a></div>"
    )

    def get(self, url, *args, **kwargs):
        if url.endswith("/"):
            html = self._HOME
        elif url.endswith("/contact"):
            html = self._CONTACT
        else:
            html = ""
        return types.SimpleNamespace(
            status_code=200 if html else 404,
            text=html,
            json=lambda: {"Answer": [{"type": 15, "data": "10 mx.arringtonroofing.com"}]},
        )


class TestCrossPageDedup:

    def test_contact_page_email_merged_into_homepage_person(self, monkeypatch):
        """The same real Chris Arrington on two pages dedups to one, and the
        /contact mailto survives the merge (the homepage saw the name first).
        """
        fake = _TwoPageFake()
        monkeypatch.setattr("app.discovery.leadership_discovery.requests", fake)
        monkeypatch.setattr("app.email.domain_verifier.requests", fake)
        leaders = LeadershipDiscovery().discover("https://arringtonroofing.com")

        assert len(leaders) == 1
        chris = leaders[0]
        assert chris["person"]["name"] == "Chris Arrington"
        assert chris["person"]["role_relevance"] is True
        assert [e["email"] for e in chris["emails"]] == [
            "chris@arringtonroofing.com"
        ]
        assert chris["emails"][0]["tier"] == "person_bound"


class _StatusFake:
    """Returns one fixed status code and body for every candidate path."""

    def __init__(self, status: int, html: str = ""):
        self._status = status
        self._html = html

    def get(self, *args, **kwargs):
        return types.SimpleNamespace(
            status_code=self._status,
            text=self._html,
            json=lambda: {"Answer": []},
        )


class _RaisingFake:
    """Every fetch raises, as an unreachable host does."""

    # discover() evaluates ``requests.RequestException`` on this object when an
    # exception propagates, so the real class must be reachable through the fake.
    RequestException = requests.RequestException

    def get(self, *args, **kwargs):
        raise requests.ConnectTimeout("no route to host")


class TestScanDiagnostics:
    """roadmap D20: a scan that finds nobody must say WHY, visibly.

    Previously non-200 responses were dropped by a bare ``continue`` and transport
    errors were logged at DEBUG while logging is unconfigured, so three failures
    with three different fixes — missing paths, a server refusing the client, and
    a parser that matched nothing — were indistinguishable from the terminal. The
    2026-08-19 run reported "no named decision-maker" for 3 of 5 companies and
    left no way to tell which had happened.
    """

    def _scan(self, monkeypatch, fake, website="https://example-roofing.com"):
        monkeypatch.setattr("app.discovery.leadership_discovery.requests", fake)
        monkeypatch.setattr("app.email.domain_verifier.requests", fake)
        discovery = LeadershipDiscovery()
        leaders = discovery.discover(website)
        return discovery.last_scan, leaders

    def test_all_paths_missing_is_diagnosed_as_missing(self, monkeypatch):
        stats, leaders = self._scan(monkeypatch, _StatusFake(404))
        assert leaders == []
        assert stats.pages_ok == 0
        assert stats.pages_attempted == len(LeadershipDiscovery.CANDIDATE_PAGES)
        assert stats.missing_pages == stats.pages_attempted
        assert stats.verdict.startswith("missing")

    def test_refused_client_is_diagnosed_as_blocked(self, monkeypatch):
        stats, _ = self._scan(monkeypatch, _StatusFake(403))
        assert stats.blocked_pages == stats.pages_attempted
        assert stats.verdict.startswith("blocked")

    def test_server_error_on_every_path_is_blocked_not_missing(self, monkeypatch):
        """boldroofing.com answered 500 on every path in the live run. A site that
        breaks identically on nine unrelated paths is refusing this client, not
        broken nine separate times, so 5xx counts as blocked."""
        stats, _ = self._scan(monkeypatch, _StatusFake(500))
        assert stats.blocked_pages == stats.pages_attempted
        assert stats.verdict.startswith("blocked")

    def test_fetched_but_unparsed_is_diagnosed_as_parser(self, monkeypatch):
        """200 on every page with nobody found is the parser's problem, and must
        not be reported as missing pages."""
        html = "<html><body><p>Call us for a free estimate.</p></body></html>"
        stats, leaders = self._scan(monkeypatch, _StatusFake(200, html))
        assert leaders == []
        assert stats.pages_ok == stats.pages_attempted
        assert stats.candidates == 0
        assert stats.verdict.startswith("parser")

    def test_transport_failure_is_counted_by_exception_type(self, monkeypatch):
        stats, _ = self._scan(monkeypatch, _RaisingFake())
        assert stats.pages_ok == 0
        assert stats.error_counts == {"ConnectTimeout": stats.pages_attempted}
        assert stats.verdict.startswith("transport")

    def test_empty_scan_is_logged_at_warning(self, monkeypatch, caplog):
        """WARNING, not INFO: logging is unconfigured in run_leads, so Python's
        last-resort handler emits only WARNING and above. An INFO line would be
        invisible in exactly the case that needs diagnosing."""
        logger_name = "app.discovery.leadership_discovery"
        with caplog.at_level(logging.WARNING, logger=logger_name):
            self._scan(monkeypatch, _StatusFake(404))
        warnings = [
            record
            for record in caplog.records
            if record.name == logger_name and record.levelno == logging.WARNING
        ]
        assert warnings, "an empty scan must be visible at WARNING"
        assert "LeadershipDiscovery" in warnings[0].getMessage()

    def test_successful_scan_is_not_warned_about(self, monkeypatch, caplog):
        logger_name = "app.discovery.leadership_discovery"
        with caplog.at_level(logging.WARNING, logger=logger_name):
            stats, leaders = self._scan(
                monkeypatch, _FakeRequests(), "https://texasskylineco.com"
            )
        assert stats.people == len(leaders)
        assert stats.people > 0
        assert stats.verdict == "ok"
        # Scoped to this logger: a warning from some other module during a
        # successful scan is a different question, and asserting on it here would
        # make this test fail for an unrelated reason.
        assert not [
            record
            for record in caplog.records
            if record.name == logger_name and record.levelno >= logging.WARNING
        ]

    def test_stats_are_serialisable_for_reports(self, monkeypatch):
        """as_dict() is what a future report/export consumes, so it must carry the
        verdict alongside the raw counters."""
        stats, _ = self._scan(monkeypatch, _StatusFake(404))
        payload = stats.as_dict()
        assert payload["website"] == "https://example-roofing.com"
        assert payload["people"] == 0
        assert payload["verdict"].startswith("missing")
        assert payload["status_counts"] == {404: stats.pages_attempted}
