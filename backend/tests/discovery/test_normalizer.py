"""Tests for Normalizer."""

from __future__ import annotations

import pytest

from app.engines.source_connectors.normalizer import Normalizer
from app.engines.source_connectors.sdk import CompanyResult


class TestNormalizer:

    def test_normalize_valid_record(self):
        norm = Normalizer()
        result = norm.normalize({
            "company_name": "  Acme Corp  ",
            "website": "https://acme.com",
            "city": "Dallas",
            "state": "TX",
        })
        assert result is not None
        assert result.company_name == "Acme Corp"
        assert result.website == "https://acme.com"
        assert result.city == "Dallas"
        assert result.state == "TX"
        assert result.country == "USA"

    def test_normalize_empty_name_returns_none(self):
        norm = Normalizer()
        assert norm.normalize({"website": "https://x.com"}) is None

    def test_normalize_empty_website_returns_none(self):
        norm = Normalizer()
        assert norm.normalize({"company_name": "Acme"}) is None

    def test_normalize_defaults_country(self):
        norm = Normalizer()
        result = norm.normalize({
            "company_name": "Test",
            "website": "https://test.com",
            "city": "A",
            "state": "B",
        })
        assert result is not None
        assert result.country == "USA"

    def test_normalize_extra_fields(self):
        norm = Normalizer()
        result = norm.normalize({
            "company_name": "Acme",
            "website": "https://acme.com",
            "city": "Dallas",
            "state": "TX",
            "extra_field": "value",
        })
        assert result is not None
        assert result.extra == {"extra_field": "value"}

    def test_normalize_batch(self):
        norm = Normalizer()
        records = [
            {"company_name": "A", "website": "https://a.com", "city": "X", "state": "Y"},
            {"website": "https://b.com"},  # missing name → skipped
            {"company_name": "C", "website": "https://c.com", "city": "Z", "state": "W"},
        ]
        results, skipped = norm.normalize_batch(records)
        assert len(results) == 2
        assert skipped == 1

    def test_strip_suffix_inc(self):
        # Iterative stripping removes ALL trailing suffixes
        assert Normalizer.strip_suffix("Acme Corp Inc.") == "Acme"
        assert Normalizer.strip_suffix("Acme Corporation") == "Acme"

    def test_strip_suffix_llc(self):
        assert Normalizer.strip_suffix("Acme Construction LLC") == "Acme Construction"

    def test_strip_suffix_no_match(self):
        assert Normalizer.strip_suffix("Acme Building") == "Acme Building"

    def test_normalised_name_key(self):
        key1 = Normalizer.normalised_name_key("Acme Corp Inc.")
        key2 = Normalizer.normalised_name_key("Acme Corp")
        assert key1 == key2  # Both strip to "acme"
        key3 = Normalizer.normalised_name_key("Acme Construction LLC")
        key4 = Normalizer.normalised_name_key("Acme Construction")
        assert key3 == key4  # No suffix to strip, same name

    def test_case_insensitive_suffix(self):
        # "Acme Corp llc" — both Corp and llc are stripped iteratively
        assert Normalizer.strip_suffix("Acme Corp llc") == "Acme"
