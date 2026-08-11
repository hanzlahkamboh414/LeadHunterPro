"""Offline tests for the Increment-3 email domain/MX verifier.

Proves the ``domain`` tier is produced honestly: a ``format`` address is
upgraded to ``domain`` ONLY when its domain's MX record resolves; unresolvable
domains, non-200 responses, and network errors keep it ``format``; and
``person_bound`` addresses are never re-verified or downgraded. ``requests``
is monkeypatched throughout — no live resolver is ever hit (CLAUDE.md §1).
"""

from __future__ import annotations

import types

import requests

from app.email.domain_verifier import domain_has_mx, verify_email_domains
from app.engines.lead.lead_models import EmailVerificationTier, LeadEmail

MX_ANSWER = [
    {"name": "texasskylineco.com", "type": 15, "TTL": 300, "data": "10 mx.texasskylineco.com"}
]


class _FakeRequests:
    """Canned ``requests`` that records every lookup and raises on demand."""

    def __init__(self, payload=None, status=200, error=None):
        self.payload = payload
        self.status = status
        self.error = error
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=10):
        self.calls.append(params["name"] if params else url)
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(status_code=self.status, json=lambda: self.payload)


def _patch(monkeypatch, fake):
    monkeypatch.setattr("app.email.domain_verifier.requests", fake)
    return fake


def _emails(*pairs):
    return [LeadEmail(email=email, tier=tier) for email, tier in pairs]


class TestDomainHasMx:

    def test_mx_answer_is_true(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        assert domain_has_mx("texasskylineco.com") is True
        assert fake.calls == ["texasskylineco.com"]

    def test_no_answer_is_false(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests({"Answer": []}))
        assert domain_has_mx("texasskylineco.com") is False

    def test_nxdomain_is_false(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests({"Status": 3}))
        assert domain_has_mx("fabricated.example.com") is False

    def test_only_a_record_is_not_mx(self, monkeypatch):
        _patch(
            monkeypatch,
            _FakeRequests({"Answer": [{"type": 1, "data": "93.184.216.34"}]}),
        )
        assert domain_has_mx("texasskylineco.com") is False

    def test_non_200_is_false(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests({}, status=500))
        assert domain_has_mx("texasskylineco.com") is False

    def test_network_error_is_false(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests(error=requests.ConnectionError("down")))
        assert domain_has_mx("texasskylineco.com") is False

    def test_non_domain_string_rejected_without_lookup(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        assert domain_has_mx("localhost") is False
        assert domain_has_mx("not a domain") is False
        assert fake.calls == []


class TestVerifyEmailDomains:

    def test_format_email_with_mx_upgrades_to_domain(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        result = verify_email_domains(
            _emails(("info@texasskylineco.com", EmailVerificationTier.format))
        )
        assert result[0].tier is EmailVerificationTier.domain
        assert result[0].email == "info@texasskylineco.com"

    def test_format_email_without_mx_stays_format(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests({"Answer": []}))
        result = verify_email_domains(
            _emails(("info@fabricated.example.com", EmailVerificationTier.format))
        )
        assert result[0].tier is EmailVerificationTier.format

    def test_person_bound_email_untouched_and_not_looked_up(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        result = verify_email_domains(
            _emails(("m.gomez@texasskylineco.com", EmailVerificationTier.person_bound))
        )
        assert result[0].tier is EmailVerificationTier.person_bound
        assert fake.calls == []  # person_bound is never re-verified

    def test_one_lookup_per_unique_domain(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        result = verify_email_domains(
            _emails(
                ("info@texasskylineco.com", EmailVerificationTier.format),
                ("office@texasskylineco.com", EmailVerificationTier.format),
                ("sales@otherco.com", EmailVerificationTier.format),
            )
        )
        assert fake.calls.count("texasskylineco.com") == 1
        assert fake.calls.count("otherco.com") == 1
        assert [e.tier for e in result] == [
            EmailVerificationTier.domain,
            EmailVerificationTier.domain,
            EmailVerificationTier.domain,
        ]

    def test_network_failure_keeps_format(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests(error=requests.ConnectionError("down")))
        result = verify_email_domains(
            _emails(("info@texasskylineco.com", EmailVerificationTier.format))
        )
        assert result[0].tier is EmailVerificationTier.format

    def test_malformed_domain_not_looked_up(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        result = verify_email_domains(
            _emails(("info@localhost", EmailVerificationTier.format))
        )
        assert result[0].tier is EmailVerificationTier.format
        assert fake.calls == []

    def test_upgrade_preserves_source_url_and_fetched_at(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        stamp = "2026-08-10T00:00:00+00:00"
        email = LeadEmail(
            email="info@texasskylineco.com",
            tier=EmailVerificationTier.format,
            source_url="https://texasskylineco.com/team",
            fetched_at=stamp,
        )
        result = verify_email_domains([email])
        assert result[0].tier is EmailVerificationTier.domain
        assert result[0].source_url == "https://texasskylineco.com/team"
        assert result[0].fetched_at == stamp

    def test_input_list_not_mutated(self, monkeypatch):
        _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        original = _emails(("info@texasskylineco.com", EmailVerificationTier.format))
        verify_email_domains(original)
        assert original[0].tier is EmailVerificationTier.format

    def test_empty_list_makes_no_lookups(self, monkeypatch):
        fake = _patch(monkeypatch, _FakeRequests({"Answer": MX_ANSWER}))
        assert verify_email_domains([]) == []
        assert fake.calls == []
