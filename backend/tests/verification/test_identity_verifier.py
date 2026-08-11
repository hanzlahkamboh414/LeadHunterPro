"""Phase 2 Step 1 — deterministic identity + official-website verification.

Every test is fully offline: the network fetcher is replaced with a stub so
no DNS/TCP occurs. Pins the accuracy-first behaviour:

- syntactic URL validity is NOT verification (accuracy Rule 4);
- reserved / placeholder / aggregator / directory-member URLs are rejected;
- an unreachable website is ``unknown``, never proof the company is fake;
- domain <-> name overlap + name-on-page evidence confirm the official site;
- no query/location context can ever become evidence (verify() has no such
  parameters and never invents a website);
- identity verification never judges business type (that is Phase 2 Step 2).
"""

from __future__ import annotations

from app.engines.verification.identity_verifier import (
    FetchOutcome,
    IdentityVerifier,
)
from app.engines.verification.models import VerificationStatus


class _StubFetcher:
    """Deterministic fetcher stub — serves canned liveness + page text."""

    def __init__(self, *, ok=True, status_code=200, page_text=""):
        self._ok = ok
        self._status_code = status_code
        self._page_text = page_text
        self.called_urls = []

    def __call__(self, url):
        self.called_urls.append(url)
        return FetchOutcome(
            ok=self._ok,
            status_code=self._status_code,
            page_text=self._page_text,
        )


def _verify(
    name,
    website,
    *,
    source="",
    source_url="",
    source_type=None,
    ok=True,
    status_code=200,
    page_text="",
):
    fetcher = _StubFetcher(ok=ok, status_code=status_code, page_text=page_text)
    verifier = IdentityVerifier(fetcher=fetcher)
    result = verifier.verify(
        company_name=name,
        website=website,
        source=source,
        source_url=source_url,
        source_type=source_type,
    )
    return result, fetcher


# ---------------------------------------------------------------------------
# Rejected URLs (no fetch should even occur)
# ---------------------------------------------------------------------------


class TestRejectedUrls:
    """Placeholder / reserved / malformed / aggregator URLs are rejected."""

    @staticmethod
    def _assert_rejected(result, fetcher, *, expect_no_fetch=True):
        assert result.website_status == VerificationStatus.rejected
        assert result.official_site_confirmed is False
        assert result.identity_status == VerificationStatus.unknown
        assert result.confidence == 0.0
        if expect_no_fetch:
            assert fetcher.called_urls == []

    def test_invalid_tld_rejected(self):
        """``.invalid`` (RFC 6761) is never a real company site."""
        result, fetcher = _verify("Acme Roofing LLC", "https://acme.invalid")
        self._assert_rejected(result, fetcher)

    def test_example_tld_rejected(self):
        """``.example`` (RFC 6761) is never a real company site."""
        result, fetcher = _verify("Acme Roofing LLC", "https://acme.example")
        self._assert_rejected(result, fetcher)

    def test_example_com_rejected(self):
        """``example.com`` is a placeholder, not an official website."""
        result, fetcher = _verify("Acme Roofing LLC", "https://example.com")
        self._assert_rejected(result, fetcher)

    def test_placeholder_url_rejected(self):
        """``yourwebsite.com``-style placeholders are rejected."""
        result, fetcher = _verify("Acme Roofing LLC", "https://www.yourwebsite.com")
        self._assert_rejected(result, fetcher)

    def test_malformed_url_rejected(self):
        """Malformed addresses are rejected, not treated as verified."""
        for bad in ("not a url", "http://", "mailto:info@acme.com"):
            result, fetcher = _verify("Acme Roofing LLC", bad)
            self._assert_rejected(result, fetcher)

    def test_directory_member_url_not_official(self):
        """An aggregator listing page is not the company's official site."""
        url = "https://www.yellowpages.com/dallas-tx/mip/acme-roofing-5980123"
        result, fetcher = _verify("Acme Roofing LLC", url)
        self._assert_rejected(result, fetcher)

    def test_directory_path_without_domain_overlap_not_official(self):
        """A /member/ page on an unrelated host is a listing, not official."""
        url = "https://www.directorysite.org/member/acme-roofing"
        result, fetcher = _verify("Acme Roofing LLC", url)
        self._assert_rejected(result, fetcher)


# ---------------------------------------------------------------------------
# Official-site confirmation
# ---------------------------------------------------------------------------


class TestOfficialSiteConfirmation:
    def test_domain_name_overlap_accepted(self):
        """Domain<->name overlap + name-on-page confirms the official site."""
        result, fetcher = _verify(
            "Acme Roofing LLC",
            "https://www.acmeroofing.com",
            page_text="Acme Roofing provides commercial roofing in Dallas, TX.",
        )
        assert result.website_status == VerificationStatus.verified
        assert result.official_site_confirmed is True
        assert result.domain_overlap == 1.0
        assert result.identity_status == VerificationStatus.verified
        assert result.confidence > 0.5
        # The fetcher is handed the canonical URL, not the raw input.
        assert fetcher.called_urls == ["https://acmeroofing.com/"]

    def test_valid_official_site_produces_field_evidence(self):
        """A confirmed site emits website + company_name FieldEvidence."""
        result, _ = _verify(
            "Acme Roofing LLC",
            "https://acmeroofing.com",
            source="business_directory",
            source_url="https://directory.example/acme",
            page_text="Acme Roofing LLC is a licensed Dallas roofing contractor.",
        )
        fields = {e.field for e in result.evidence}
        assert fields == {"website", "company_name"}
        website_ev = next(e for e in result.evidence if e.field == "website")
        assert website_ev.value == "https://acmeroofing.com/"
        assert website_ev.is_reliable is True
        assert website_ev.confidence == 1.0
        assert website_ev.source == "business_directory"
        assert website_ev.source_url == "https://directory.example/acme"

    def test_multiword_domain_single_label_overlap(self):
        """``atlasroofing.com`` for "Atlas Roofing LLC" overlaps fully."""
        result, _ = _verify(
            "Atlas Roofing LLC",
            "https://www.atlasroofing.com",
            page_text="Atlas Roofing serves the DFW metroplex.",
        )
        assert result.official_site_confirmed is True
        assert result.domain_overlap == 1.0


# ---------------------------------------------------------------------------
# Not-verified cases (no false "yes", no invented evidence)
# ---------------------------------------------------------------------------


class TestNotVerified:
    def test_obvious_domain_name_mismatch_not_verified(self):
        """A live page for a different company does NOT verify identity."""
        result, fetcher = _verify(
            "Acme Roofing LLC",
            "https://www.bestshingles.com",
            page_text="Best Shingles roofing products company.",
        )
        assert result.website_status == VerificationStatus.unknown
        assert result.official_site_confirmed is False
        assert result.identity_status == VerificationStatus.unknown
        assert result.confidence == 0.0
        assert fetcher.called_urls  # the page WAS fetched (and failed the check)

    def test_unreachable_website_not_verified(self):
        """Unreachable => unknown, NOT fake, and NOT verified."""
        result, fetcher = _verify("Acme Roofing LLC", "https://acmeroofing.com", ok=False)
        assert result.website_status == VerificationStatus.unknown
        assert result.official_site_confirmed is False
        assert result.identity_status == VerificationStatus.unknown
        assert result.confidence == 0.0
        assert any("unreachable" in r for r in result.reasons)
        # The fetcher is handed the canonical URL, not the raw input.
        assert fetcher.called_urls == ["https://acmeroofing.com/"]

    def test_syntax_valid_reachable_but_no_name_evidence(self):
        """A valid, reachable URL with no name on the page is NOT verified."""
        result, _ = _verify("Acme Roofing LLC", "https://acmeroofing.com", page_text="")
        assert result.website_status == VerificationStatus.unknown
        assert result.official_site_confirmed is False
        assert result.confidence == 0.0

    def test_no_query_derived_evidence(self):
        """verify() accepts no location/query — nothing is invented from it."""
        result, _ = _verify("Acme Roofing LLC", "", source="")
        assert result.website_status == VerificationStatus.unknown
        assert result.identity_status == VerificationStatus.unknown
        assert result.confidence == 0.0
        assert result.normalized_website == ""
        assert result.evidence == []

    def test_empty_or_single_char_name_not_verified(self):
        """A non-credible name yields no identity claim."""
        for bad_name in ("", "X"):
            result, _ = _verify(bad_name, "https://acmeroofing.com",
                                page_text="Acme Roofing LLC roofing.")
            assert result.name_verified is False
            assert result.identity_status == VerificationStatus.unknown


# ---------------------------------------------------------------------------
# Source-tier corroboration + boundary
# ---------------------------------------------------------------------------


class TestTierCorroboration:
    def test_tier1_source_corroborates_name_without_site(self):
        """A Tier 1 source (e.g. licensing board) corroborates the name."""
        result, _ = _verify(
            "Acme Roofing LLC",
            "",
            source="licensing_board",
            source_url="https://tcb.state.example/lic/acme",
        )
        assert result.name_verified is True
        assert result.identity_status == VerificationStatus.partially_verified
        assert result.confidence == 0.3

    def test_unclassified_source_does_not_corroborate_name(self):
        """An unclassified/unknown source alone does NOT verify the name."""
        result, _ = _verify("Acme Roofing LLC", "", source="mystery_source")
        assert result.name_verified is False
        assert result.identity_status == VerificationStatus.unknown
        assert result.confidence == 0.0


class TestIdentityBoundary:
    def test_identity_verification_does_not_judge_business_type(self):
        """Owens Corning is identity-verified; business type is Phase 2 Step 2."""
        result, _ = _verify(
            "Owens Corning",
            "https://www.owenscorning.com",
            page_text="Owens Corning roofing materials manufacturer.",
        )
        assert result.official_site_confirmed is True
        assert result.identity_status == VerificationStatus.verified
        # The identity verifier must NOT reject manufacturers — that decision
        # belongs to the industry/business-type verifier (Step 2).
        assert result.website_status == VerificationStatus.verified
