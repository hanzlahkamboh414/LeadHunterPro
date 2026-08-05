"""Infrastructure tests for the Direct Website Discovery duplicate URL filter.

Phase 2.3A scope: URL-level filtering only. Nothing here fetches, parses
or crawls; the filter never sees a page, only candidate strings.
"""

from __future__ import annotations

import pytest

from app.discovery.website.url_filter import DuplicateURLFilter, FilterStats


@pytest.fixture
def url_filter():
    return DuplicateURLFilter()


class TestAccept:
    def test_new_url_is_returned_canonicalized(self, url_filter):
        assert url_filter.accept("acme.com") == "https://acme.com/"

    def test_repeat_of_the_same_url_is_rejected(self, url_filter):
        url_filter.accept("https://acme.com/")
        assert url_filter.accept("https://acme.com/") is None

    def test_distinct_urls_are_both_accepted(self, url_filter):
        assert url_filter.accept("acme.com") is not None
        assert url_filter.accept("globex.com") is not None
        assert len(url_filter) == 2


class TestVariantCollapsing:
    @pytest.mark.parametrize(
        "variant",
        [
            "http://acme.com/",
            "https://www.acme.com",
            "http://www.acme.com/",
            "acme.com",
            "//acme.com/",
            "HTTPS://ACME.COM/",
            "https://acme.com:443/",
        ],
    )
    def test_variants_of_one_site_collapse_to_one_entry(self, url_filter, variant):
        url_filter.accept("https://acme.com/")
        assert url_filter.accept(variant) is None
        assert len(url_filter) == 1

    def test_different_hosts_do_not_collapse(self, url_filter):
        url_filter.accept("acme.com")
        assert url_filter.accept("acme.net") is not None

    def test_different_paths_do_not_collapse(self, url_filter):
        url_filter.accept("acme.com/about")
        assert url_filter.accept("acme.com/contact") is not None

    def test_different_query_strings_do_not_collapse(self, url_filter):
        url_filter.accept("acme.com/p?id=1")
        assert url_filter.accept("acme.com/p?id=2") is not None


class TestFirstSeenWins:
    def test_original_scheme_is_kept(self, url_filter):
        # The http variant arrived first and is known to have been offered
        # by a real source; the filter must not rewrite it to https.
        assert url_filter.accept("http://acme.com/") == "http://acme.com/"
        assert url_filter.accept("https://acme.com/") is None
        assert url_filter.accepted == ["http://acme.com/"]

    def test_later_variant_does_not_replace_the_stored_url(self, url_filter):
        url_filter.accept("http://www.acme.com/")
        url_filter.accept("https://acme.com")
        assert url_filter.accepted == ["http://acme.com/"]


class TestInvalidInput:
    @pytest.mark.parametrize(
        "value",
        ["", "   ", "mailto:sales@acme.com", "tel:+15551234567", "https://", "ftp://x.com"],
    )
    def test_unusable_candidates_are_rejected(self, url_filter, value):
        assert url_filter.accept(value) is None
        assert len(url_filter) == 0

    def test_invalid_is_counted_separately_from_duplicate(self, url_filter):
        url_filter.accept("acme.com")
        url_filter.accept("acme.com")
        url_filter.accept("mailto:a@b.com")
        stats = url_filter.stats
        assert stats.duplicates == 1
        assert stats.invalid == 1

    def test_repeated_invalid_urls_are_never_deduplicated(self, url_filter):
        url_filter.accept("mailto:a@b.com")
        url_filter.accept("mailto:a@b.com")
        assert url_filter.stats.invalid == 2
        assert url_filter.stats.duplicates == 0


class TestFilterBatch:
    def test_order_is_preserved(self, url_filter):
        out = url_filter.filter(["c.com", "a.com", "b.com"])
        assert out == ["https://c.com/", "https://a.com/", "https://b.com/"]

    def test_duplicates_are_removed_across_the_batch(self, url_filter):
        out = url_filter.filter(["acme.com", "http://www.acme.com/", "globex.com"])
        assert out == ["https://acme.com/", "https://globex.com/"]

    def test_empty_input_yields_empty_output(self, url_filter):
        assert url_filter.filter([]) == []

    def test_state_carries_across_calls(self, url_filter):
        url_filter.filter(["acme.com"])
        assert url_filter.filter(["acme.com", "globex.com"]) == ["https://globex.com/"]

    def test_accepts_a_generator(self, url_filter):
        assert url_filter.filter(u for u in ["acme.com", "acme.com"]) == [
            "https://acme.com/"
        ]


class TestStats:
    def test_counts_reflect_every_outcome(self, url_filter):
        url_filter.filter(["acme.com", "http://acme.com", "mailto:a@b.com", "globex.com"])
        assert url_filter.stats == FilterStats(
            total=4, accepted=2, duplicates=1, invalid=1
        )

    def test_fresh_filter_is_all_zeroes(self, url_filter):
        assert url_filter.stats == FilterStats()

    def test_stats_serialize_for_metadata(self, url_filter):
        url_filter.accept("acme.com")
        assert url_filter.stats.to_dict() == {
            "total": 1,
            "accepted": 1,
            "duplicates": 0,
            "invalid": 0,
        }


class TestIsDuplicate:
    def test_reports_accepted_urls(self, url_filter):
        url_filter.accept("acme.com")
        assert url_filter.is_duplicate("http://www.acme.com/") is True

    def test_reports_unseen_urls_as_new(self, url_filter):
        assert url_filter.is_duplicate("acme.com") is False

    def test_invalid_url_is_not_a_duplicate(self, url_filter):
        assert url_filter.is_duplicate("mailto:a@b.com") is False

    def test_checking_does_not_record_or_count(self, url_filter):
        url_filter.accept("acme.com")
        before = url_filter.stats
        url_filter.is_duplicate("globex.com")
        url_filter.is_duplicate("acme.com")
        assert url_filter.stats == before
        assert len(url_filter) == 1


class TestReset:
    def test_clears_accepted_urls_and_counters(self, url_filter):
        url_filter.filter(["acme.com", "acme.com", "mailto:a@b.com"])
        url_filter.reset()
        assert len(url_filter) == 0
        assert url_filter.stats == FilterStats()

    def test_previously_seen_url_is_accepted_again(self, url_filter):
        url_filter.accept("acme.com")
        url_filter.reset()
        assert url_filter.accept("acme.com") == "https://acme.com/"


class TestContainerProtocol:
    def test_len_counts_distinct_urls(self, url_filter):
        url_filter.filter(["acme.com", "http://acme.com", "globex.com"])
        assert len(url_filter) == 2

    def test_contains_matches_variants(self, url_filter):
        url_filter.accept("acme.com")
        assert "http://www.acme.com/" in url_filter
        assert "globex.com" not in url_filter

    def test_contains_is_safe_for_non_strings(self, url_filter):
        assert 42 not in url_filter
        assert None not in url_filter

    def test_iteration_yields_first_seen_order(self, url_filter):
        url_filter.filter(["c.com", "a.com"])
        assert list(url_filter) == ["https://c.com/", "https://a.com/"]

    def test_repr_reports_outcomes(self, url_filter):
        url_filter.filter(["acme.com", "acme.com"])
        text = repr(url_filter)
        assert "accepted=1" in text
        assert "duplicates=1" in text
