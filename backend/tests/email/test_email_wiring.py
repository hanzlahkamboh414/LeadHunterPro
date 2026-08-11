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
        raw = ["Mix@Example.COM", "mix@example.com", "bad@@example.com", "user@localhost"]
        assert clean_emails(raw) == ["mix@example.com"]

    def test_clean_emails_handles_default_empty(self):
        assert clean_emails([]) == []


class TestWebsiteEngineWiring:
    """WebsiteEngine.parse now emits validated, lowercased, deduped emails."""

    def test_parse_cleans_emails(self):
        html = (
            "<html><head><title>Acme</title></head><body>"
            "Reach Jane@Example.COM or jane@example.com, but bad@@ stays out"
            "</body></html>"
        )
        parsed = WebsiteEngine().parse(html, "https://acme.example.com")
        assert parsed["emails"] == ["jane@example.com"]

    def test_parse_deduplicates_case_variants(self):
        html = (
            "<html><body>John@Example.COM john@example.com</body></html>"
        )
        parsed = WebsiteEngine().parse(html, "https://acme.example.com")
        assert parsed["emails"] == ["john@example.com"]


class TestEmailDiscoveryWiring:
    """EmailDiscovery.discover filters malformed addresses offline."""

    def test_discover_returns_uniform_emails(self, monkeypatch):
        html = (
            "<html>"
            "John@Example.COM john@example.com "
            'mailto:<a href="mailto:Info@Example.COM?subject=Hi">info</a> '
            "bad@@example.com user@localhost "
            '<a href="mailto:not-an-email">broken</a>'
            "</html>"
        )

        class _FakeRequests:
            def get(self, url, timeout=10, headers=None):
                return _response(html)

        monkeypatch.setattr(
            "app.email.email_discovery.requests", _FakeRequests()
        )
        result = EmailDiscovery().discover("https://acme.example.com")
        assert result["emails"] == ["info@example.com", "john@example.com"]
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
                    "emails": ["Mix@Example.COM", "bad@@example.com", "mix@example.com"],
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
        assert captured["emails"] == ["mix@example.com"]