"""Offline tests for the Increment-3 domain-tier wiring in LeadershipDiscovery.

The saved team-page fixture is served for every candidate URL and ``requests``
is monkeypatched for BOTH the page fetch and the MX lookup, so the whole
discover() path runs offline. Proves a generic mailbox bound to a person is
upgraded to ``domain`` when its domain resolves, while every ``person_bound``
address keeps its tier.
"""

from __future__ import annotations

import types

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
