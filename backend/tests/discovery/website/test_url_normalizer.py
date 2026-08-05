"""Infrastructure tests for the Direct Website Discovery URL normalizer.

Phase 2.3A scope: pure string canonicalization. Nothing here performs
HTTP, DNS, parsing or crawling, and no test asserts that a URL resolves.
"""

from __future__ import annotations

import pytest

from app.discovery.website.url_normalizer import (
    ALLOWED_SCHEMES,
    DEFAULT_SCHEME,
    canonical_key,
    extract_host,
    normalize_url,
)


class TestSchemeHandling:
    def test_bare_domain_gets_default_scheme(self):
        assert normalize_url("acme.com") == f"{DEFAULT_SCHEME}://acme.com/"

    def test_bare_domain_with_path(self):
        assert normalize_url("acme.com/about") == f"{DEFAULT_SCHEME}://acme.com/about"

    def test_protocol_relative_gets_default_scheme(self):
        assert normalize_url("//acme.com/x") == f"{DEFAULT_SCHEME}://acme.com/x"

    def test_http_is_preserved_not_upgraded(self):
        # Upgrading would fabricate a URL that an http-only host may not answer.
        assert normalize_url("http://acme.com/x") == "http://acme.com/x"

    def test_https_is_preserved(self):
        assert normalize_url("https://acme.com/x") == "https://acme.com/x"

    def test_scheme_case_is_normalized(self):
        assert normalize_url("HTTP://acme.com/x") == "http://acme.com/x"

    def test_only_http_schemes_are_allowed(self):
        assert ALLOWED_SCHEMES == {"http", "https"}


class TestRejection:
    @pytest.mark.parametrize(
        "value",
        ["", "   ", "\n", "https://", "http://", "//", "/just/a/path"],
    )
    def test_unusable_input_returns_none(self, value):
        assert normalize_url(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            "mailto:sales@acme.com",
            "tel:+15551234567",
            "javascript:void(0)",
            "ftp://files.acme.com",
            "data:text/html,hello",
            "file:///c:/tmp/x.html",
        ],
    )
    def test_non_crawlable_schemes_are_rejected(self, value):
        # A bare "://" check would misread "mailto:" as scheme-less and
        # produce "https://mailto:sales@acme.com".
        assert normalize_url(value) is None

    def test_out_of_range_port_is_rejected(self):
        assert normalize_url("https://acme.com:99999/x") is None

    def test_rejection_is_consistent_across_all_three_functions(self):
        assert normalize_url("mailto:a@b.com") is None
        assert canonical_key("mailto:a@b.com") is None
        assert extract_host("mailto:a@b.com") is None


class TestHostCanonicalization:
    def test_host_case_is_lowered(self):
        assert normalize_url("https://ACME.com/x") == "https://acme.com/x"

    def test_www_is_stripped(self):
        assert normalize_url("https://www.acme.com/x") == "https://acme.com/x"

    def test_www_is_not_stripped_when_it_would_leave_a_bare_tld(self):
        # "www.com" is a registrable domain; stripping leaves "com".
        assert normalize_url("https://www.com/") == "https://www.com/"

    def test_nested_www_subdomain_strips_only_the_prefix(self):
        assert normalize_url("https://www.mail.acme.com/") == "https://mail.acme.com/"

    def test_trailing_dot_in_host_is_removed(self):
        assert normalize_url("https://acme.com./x") == "https://acme.com/x"

    def test_credentials_are_dropped(self):
        assert normalize_url("https://user:pass@acme.com/x") == "https://acme.com/x"

    def test_default_port_is_removed(self):
        assert normalize_url("http://acme.com:80/x") == "http://acme.com/x"
        assert normalize_url("https://acme.com:443/x") == "https://acme.com/x"

    def test_non_default_port_is_preserved(self):
        assert normalize_url("https://acme.com:8443/x") == "https://acme.com:8443/x"

    def test_https_on_port_80_is_not_treated_as_default(self):
        assert normalize_url("https://acme.com:80/x") == "https://acme.com:80/x"


class TestPathCanonicalization:
    @pytest.mark.parametrize("value", ["acme.com", "acme.com/", "acme.com//"])
    def test_root_paths_all_canonicalize_to_slash(self, value):
        assert normalize_url(value) == f"{DEFAULT_SCHEME}://acme.com/"

    def test_trailing_slash_is_dropped_from_a_real_path(self):
        assert normalize_url("acme.com/about/") == f"{DEFAULT_SCHEME}://acme.com/about"

    def test_path_case_is_preserved(self):
        # Paths are case-sensitive to the server; hosts are not.
        assert normalize_url("https://acme.com/About") == "https://acme.com/About"


class TestQueryAndFragment:
    def test_fragment_is_dropped(self):
        assert normalize_url("https://acme.com/p#team") == "https://acme.com/p"

    def test_query_is_preserved(self):
        assert normalize_url("https://acme.com/p?id=12") == "https://acme.com/p?id=12"

    def test_query_order_is_not_rewritten(self):
        # Reordering can change what some servers return.
        assert normalize_url("https://acme.com/p?b=2&a=1") == "https://acme.com/p?b=2&a=1"

    def test_query_distinguishes_two_pages(self):
        assert canonical_key("acme.com/p?id=1") != canonical_key("acme.com/p?id=2")


class TestCanonicalKey:
    def test_scheme_is_collapsed(self):
        assert canonical_key("http://acme.com/x") == canonical_key("https://acme.com/x")

    def test_www_and_trailing_slash_are_collapsed(self):
        assert canonical_key("http://www.acme.com/") == canonical_key("https://acme.com")

    def test_key_is_not_a_url(self):
        assert canonical_key("https://acme.com/") == "acme.com/"

    def test_key_includes_query(self):
        assert canonical_key("https://acme.com/p?id=1") == "acme.com/p?id=1"

    def test_different_hosts_do_not_collapse(self):
        assert canonical_key("acme.com") != canonical_key("acme.net")


class TestExtractHost:
    def test_host_is_canonical(self):
        assert extract_host("https://WWW.Acme.com/contact") == "acme.com"

    def test_port_is_not_part_of_the_host(self):
        assert extract_host("https://acme.com:8443/x") == "acme.com"

    def test_pages_on_one_site_share_a_host(self):
        assert extract_host("acme.com/a") == extract_host("http://www.acme.com/b/")


class TestIdempotence:
    @pytest.mark.parametrize(
        "value",
        [
            "acme.com",
            "HTTP://WWW.Acme.com:80/About/#team",
            "https://user:pass@acme.com:8443/p/?a=1",
            "//acme.com",
        ],
    )
    def test_normalizing_twice_changes_nothing(self, value):
        once = normalize_url(value)
        assert once is not None
        assert normalize_url(once) == once

    @pytest.mark.parametrize("value", ["acme.com", "http://www.acme.com/x/"])
    def test_key_of_normalized_url_matches_key_of_raw(self, value):
        normalized = normalize_url(value)
        assert normalized is not None
        assert canonical_key(normalized) == canonical_key(value)


class TestDocumentedExamples:
    """The module docstring and doctests are part of the contract."""

    def test_module_docstring_example(self):
        assert normalize_url("HTTP://WWW.Acme.com:80/About/#team") == "http://acme.com/About"

    def test_cross_scheme_dedup_example(self):
        assert canonical_key("https://acme.com") == canonical_key("http://www.acme.com/")
