"""Offline tests for the Increment-1 email-format wiring.

Proves the existing format validators (``is_valid_email``, ``clean_emails`` in
``app/email/``) are now applied in the discovery path: ``EmailDiscovery``,
``WebsiteEngine.parse``, and ``demo.py --enrich``. Every test is offline —
the live HTTP/engine calls are monkeypatched (CLAUDE.md §1).
"""

from __future__ import annotations

import types

from app.email.email_cleaner import clean_emails
from app.email.email_discovery import EmailDiscovery
from app.email.email_validator import is_valid_email
from app.engines.website_engine import WebsiteEngine


def _response(text: str) -> types.SimpleNamespace:
    """A stand-in requests.Response with 200 status and given html."""
    return types.SimpleNamespace(status_code=200, text=text)


class TestFormatHelperContract:
    """The two existing helpers behave as the wiring relies on."""

    def test_is_valid_email_accepts_valid(self):
        assert is_valid_email("john@example.com") is True
        assert is_valid_email("m.gomez@texasskylineco.com") is True

    def test_is_valid_email_rejects_malformed(self):
        assert is_valid_email("") is False
        assert is_valid_email("not-an-email") is False
        assert is_valid_email("bad@@example.com") is False
        assert is_valid_email("user@localhost") is False  # no dotted TLD

    def test_clean_emails_filters_dedupes_lowercases(self):
        raw = ["Mix@Acme.COM", "mix@acme.com", "bad@@acme.com", "user@localhost"]
        assert clean_emails(raw) == ["mix@acme.com"]

    def test_clean_emails_handles_default_empty(self):
        assert clean_emails([]) == []

    def test_clean_emails_drops_crawl_artifacts(self):
        """Page-source machine strings (Sentry DSNs, example.com
        placeholders, mailing-list ids) never survive cleaning — observed
        live in the pending pool 2026-09-15."""
        from app.email.email_cleaner import is_crawl_artifact

        raw = [
            "owner@acme.com",  # real contact — kept
            "2062d0a4929b45348643784b5cb39c36@sentry.wixpress.com",  # Sentry DSN
            "460ff4620fa44cba8df530afde949785@sentry.wi",  # crash key
            "jane@example.com",  # form placeholder
            "20260828153349.8061-1-odion@efficios.com",  # listserv id
        ]
        assert clean_emails(raw) == ["owner@acme.com"]

        # A REAL company whose name contains "sentry" is never swept up —
        # the artifact shapes are narrow on purpose.
        assert is_crawl_artifact("karl@sentrycontracting.com") is False
        assert is_crawl_artifact("j2b@sentrigroup.net") is False


class TestWebsiteEngineWiring:
    """WebsiteEngine.parse now emits validated, lowercased, deduped emails."""

    def test_parse_cleans_emails(self):
        html = (
            "<html><head><title>Acme</title></head><body>"
            "Reach Jane@Acme.COM or jane@acme.com, but bad@@ stays out"
            "</body></html>"
        )
        parsed = WebsiteEngine().parse(html, "https://acme-contractors.com")
        assert parsed["emails"] == ["jane@acme.com"]

    def test_parse_deduplicates_case_variants(self):
        html = (
            "<html><body>John@Acme.COM john@acme.com</body></html>"
        )
        parsed = WebsiteEngine().parse(html, "https://acme-contractors.com")
        assert parsed["emails"] == ["john@acme.com"]


class TestEmailDiscoveryWiring:
    """EmailDiscovery.discover filters malformed addresses offline."""

    def test_discover_returns_uniform_emails(self, monkeypatch):
        html = (
            "<html>"
            "John@Acme.COM john@acme.com "
            'mailto:<a href="mailto:Info@Acme.COM?subject=Hi">info</a> '
            "bad@@acme.com user@localhost "
            '<a href="mailto:not-an-email">broken</a>'
            "</html>"
        )

        class _FakeRequests:
            def get(self, url, timeout=10, headers=None):
                return _response(html)

        monkeypatch.setattr(
            "app.email.email_discovery.requests", _FakeRequests()
        )
        result = EmailDiscovery().discover("https://acme-contractors.com")
        assert result["emails"] == ["info@acme.com", "john@acme.com"]
        assert result["count"] == 2


class TestDemoEnrichWiring:
    """demo.py --enrich passes emails through clean_emails before scoring."""

    def test_enrich_filters_emails(self, monkeypatch):
        captured: dict[str, list[str]] = {}

        class _FakeEngine:
            def qualify_lead(self, data, query_context):
                captured["emails"] = list(data.get("emails") or [])
                return {}

        class _FakeWebsiteEngine:
            def fetch(self, url):
                return "<html/>"

            def parse(self, html, base_url):
                return {
                    "title": "Acme",
                    "emails": ["Mix@Acme.COM", "bad@@acme.com", "mix@acme.com"],
                    "phones": [],
                    "linkedin": [],
                }

        monkeypatch.setattr("app.engines.ai_engine.AIEngine", _FakeEngine)
        monkeypatch.setattr(
            "app.engines.website_engine.WebsiteEngine", _FakeWebsiteEngine
        )

        from demo import stage_scoring

        result = types.SimpleNamespace(
            company_name="Acme",
            website="https://acme.example.com",
            city="Dallas",
            state="TX",
            metadata={
                "trade_category": "",
                "industry_focus": "",
                "discovery_reason": "",
            },
            source="live",
            source_url="https://acme.example.com",
        )
        _, enriched = stage_scoring(
            [result],
            enrich=True,
            enrich_limit=10,
            industry="Roofing",
            location="Dallas TX",
        )
        assert enriched == 1
        assert captured["emails"] == ["mix@acme.com"]