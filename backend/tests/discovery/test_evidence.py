"""Tests for Evidence model."""

from __future__ import annotations

import pytest

from app.engines.source_connectors.evidence import Evidence


class TestEvidence:

    def test_create_with_defaults(self):
        e = Evidence(source_url="https://example.com", source_name="Test")
        assert e.source_url == "https://example.com"
        assert e.source_name == "Test"
        assert e.confidence == 1.0
        assert e.raw_snippet == ""
        assert e.fetched_at == ""

    def test_create_with_all_fields(self):
        e = Evidence(
            source_url="https://example.com",
            source_name="AGC Texas",
            confidence=0.85,
            raw_snippet="Found on directory page",
            fetched_at="2026-01-15T10:30:00Z",
        )
        assert e.confidence == 0.85
        assert e.raw_snippet == "Found on directory page"
        assert e.fetched_at == "2026-01-15T10:30:00Z"

    def test_from_dict(self):
        data = {
            "source_url": "https://acme.com",
            "source_name": "Web scrape",
            "confidence": 0.9,
            "raw_snippet": "Acme Construction",
            "fetched_at": "2026-07-31T00:00:00Z",
        }
        e = Evidence.from_dict(data)
        assert e.source_url == "https://acme.com"
        assert e.confidence == 0.9

    def test_from_dict_defaults(self):
        e = Evidence.from_dict({"source_url": "x", "source_name": "y"})
        assert e.confidence == 1.0
        assert e.raw_snippet == ""

    def test_to_dict(self):
        e = Evidence(
            source_url="https://example.com",
            source_name="Test",
            confidence=0.75,
            raw_snippet="snippet",
            fetched_at="2026-01-01",
        )
        d = e.to_dict()
        assert d["source_url"] == "https://example.com"
        assert d["confidence"] == 0.75

    def test_is_reliable_below_threshold(self):
        e = Evidence(source_url="u", source_name="s", confidence=0.3)
        assert e.is_reliable(0.5) is False

    def test_is_reliable_at_threshold(self):
        e = Evidence(source_url="u", source_name="s", confidence=0.5)
        assert e.is_reliable(0.5) is True

    def test_is_reliable_above_threshold(self):
        e = Evidence(source_url="u", source_name="s", confidence=1.0)
        assert e.is_reliable(0.5) is True

    def test_is_frozen(self):
        e = Evidence(source_url="u", source_name="s")
        with pytest.raises((AttributeError, TypeError)):
            e.confidence = 0.5  # type: ignore[misc]
