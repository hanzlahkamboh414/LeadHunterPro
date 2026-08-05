"""Tests for Deduplicator."""

from __future__ import annotations

import pytest

from app.engines.source_connectors.deduplicator import Deduplicator
from app.engines.source_connectors.sdk import CompanyResult


def _company(name: str, website: str, city: str = "Dallas", state: str = "TX") -> CompanyResult:
    return CompanyResult(
        company_name=name,
        website=website,
        city=city,
        state=state,
    )


class TestDeduplicator:

    def test_deduplicate_by_domain(self):
        """Two companies on the same domain should be deduplicated."""
        results = [
            _company("Acme HQ", "https://www.acme.com"),
            _company("Acme Branch", "https://www.acme.com/locations/dallas"),
        ]
        deduped = Deduplicator().deduplicate(results)
        assert len(deduped) == 1

    def test_deduplicate_by_name(self):
        """Companies with the same normalised name should be deduplicated."""
        results = [
            _company("Acme Construction LLC", "https://acme1.com"),
            _company("Acme Construction", "https://acme2.com"),
        ]
        deduped = Deduplicator().deduplicate(results)
        assert len(deduped) == 1

    def test_no_duplicates_keeps_all(self):
        results = [
            _company("Acme", "https://acme.com"),
            _company("Beta", "https://beta.com"),
            _company("Gamma", "https://gamma.com"),
        ]
        deduped = Deduplicator().deduplicate(results)
        assert len(deduped) == 3

    def test_empty_list(self):
        assert Deduplicator().deduplicate([]) == []

    def test_single_item(self):
        results = [_company("Only", "https://only.com")]
        assert len(Deduplicator().deduplicate(results)) == 1

    def test_count_duplicates(self):
        results = [
            _company("Acme", "https://acme.com"),
            _company("Acme2", "https://acme.com/page"),
            _company("Beta", "https://beta.com"),
        ]
        assert Deduplicator.count_duplicates(results) == 1

    def test_keeps_last_when_flagged(self):
        results = [
            _company("First", "https://first.com"),
            _company("Second", "https://first.com/x"),
        ]
        deduped = Deduplicator(keep_first=False).deduplicate(results)
        assert deduped[0].company_name == "Second"

    def test_different_domains_same_name_not_deduped(self):
        """Two different companies with the same name should be kept."""
        results = [
            _company("Acme Building", "https://acme1.com"),
            _company("Acme Engineering", "https://acme2.com"),
        ]
        deduped = Deduplicator().deduplicate(results)
        # Different names, different domains — keep both
        assert len(deduped) == 2
