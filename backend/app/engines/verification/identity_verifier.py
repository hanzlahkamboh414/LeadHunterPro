"""Deterministic identity + official-website verification (Phase 2, Step 1).

Verifies, from the SUPPLIED evidence only (never from query context):

- whether a website is the company's OFFICIAL site, and
- whether the company NAME/identity is credible and matches the website/source.

A syntactically valid URL is NOT verification (accuracy Rule 4). A website
is verified only when it is reachable AND shows ownership evidence (domain
<-> name overlap and/or the company name on the page) AND is not a
placeholder, reserved, or aggregator/directory URL. An unreachable website
is ``unknown`` — it proves nothing about whether the company is real
(accuracy Rule 8: no evidence = unknown, not false). No numerical confidence
is assigned for syntax alone (accuracy Rule 11).

This step does NOT decide the company's *business type* (contractor vs
manufacturer vs supplier) — that is Phase 2 Step 2. Identity verification
is deliberately conservative and evidence-based.

Reuses existing production code (CLAUDE.md §14):
- ``app.discovery.website.url_normalizer`` — canonical URL syntax + host;
- ``app.engines.discovery.company.company_validator._is_blocked_domain`` —
  blocked / non-commercial host patterns;
- ``app.engines.verification.source_tiers`` — Tier 1-4 weighting;
- ``app.engines.verification.models`` — ``FieldEvidence`` / ``VerificationStatus``.

The default fetcher issues a single GET so both liveness AND page text are
available for the name-on-page check; tests inject a stub fetcher for
deterministic, offline runs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from app.discovery.website.url_normalizer import extract_host, normalize_url
from app.engines.discovery.company.company_validator import _is_blocked_domain
from app.engines.verification.models import FieldEvidence, VerificationStatus
from app.engines.verification.source_tiers import SourceTier, tier_for_source

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Website rejection rules
# ---------------------------------------------------------------------------

# Reserved second-level TLDs (RFC 6761) — never real company sites.
_RESERVED_SUFFIXES = (".invalid", ".example", ".test", ".localhost")

# Known placeholder / example domains.
_PLACEHOLDER_HOSTS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "yourdomain.com",
        "yourwebsite.com",
        "yourcompany.com",
        "your-site.com",
        "placeholder.com",
        "somesite.com",
        "sample.com",
        "domain.com",
        "website.com",
        "sitename.com",
    }
)
_PLACEHOLDER_TOKEN_RE = re.compile(
    r"\b(placeholder|yourdomain|yourwebsite|yourcompany|somesite|sample|example)\b",
    re.IGNORECASE,
)

# Aggregator / directory / social hosts — a page on one of these is a
# listing, never the company's official website.
_AGGREGATOR_HOSTS = frozenset(
    {
        "linkedin.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "yelp.com",
        "yellowpages.com",
        "superpages.com",
        "angieslist.com",
        "houzz.com",
        "thumbtack.com",
        "homeadvisor.com",
        "porch.com",
        "indeed.com",
        "monster.com",
        "glassdoor.com",
        "bbb.org",
        "manta.com",
        "thomasnet.com",
        "whitepages.com",
        "chamberofcommerce.com",
        "zillow.com",
        "google.com",
        "maps.google.com",
    }
)

# Path segments that mark a directory member/listing page (only treated as
# non-official when the host shows no domain<->name overlap).
_DIRECTORY_PATH_TOKENS = frozenset(
    {
        "member",
        "members",
        "profile",
        "listing",
        "listings",
        "directory",
        "biz",
        "yellowpage",
        "mip",
        "vendor",
        "company-profile",
    }
)

# Hosts must look like real domains (labels of letters/digits/hyphens).
_VALID_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
)

# Corporate / legal name suffixes ignored for domain-overlap identity.
_CORPORATE_SUFFIXES = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "limited",
        "lp",
        "llp",
        "corp",
        "corporation",
        "co",
        "company",
        "group",
        "holdings",
        "enterprise",
        "enterprises",
        "services",
        "solutions",
        "systems",
        "associates",
        "partners",
        "and",
        "the",
    }
)

# Generic trade words that must NOT alone prove "the company is on the page".
_GENERIC_TRADE_WORDS = frozenset(
    {
        "roofing",
        "roof",
        "construction",
        "contractor",
        "contractors",
        "company",
        "services",
        "solutions",
        "hvac",
        "plumbing",
        "plumber",
        "electric",
        "electrical",
        "concrete",
        "painting",
        "landscaping",
        "general",
        "commercial",
        "residential",
        "repair",
        "installation",
    }
)

# Minimum domain<->name overlap that counts as ownership evidence.
_OVERLAP_MIN = 0.3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class FetchOutcome:
    """Outcome of fetching a page for liveness + name-on-page evidence."""

    ok: bool
    status_code: int = 0
    page_text: str = ""


@dataclass
class IdentityVerificationResult:
    """Deterministic result of verifying a company's identity + official site."""

    identity_status: VerificationStatus
    website_status: VerificationStatus
    official_site_confirmed: bool
    name_verified: bool
    domain_overlap: float
    confidence: float
    reasons: list[str]
    evidence: list[FieldEvidence]
    normalized_website: str

    def to_dict(self) -> dict[str, Any]:
        """Serialisable view for downstream gates / metadata."""
        return {
            "identity_status": self.identity_status.value,
            "website_status": self.website_status.value,
            "official_site_confirmed": self.official_site_confirmed,
            "name_verified": self.name_verified,
            "domain_overlap": self.domain_overlap,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "evidence": [
                {
                    "field": e.field,
                    "value": e.value,
                    "source": e.source,
                    "source_url": e.source_url,
                    "confidence": e.confidence,
                    "fetched_at": e.fetched_at,
                    "is_reliable": e.is_reliable,
                }
                for e in self.evidence
            ],
            "normalized_website": self.normalized_website,
        }


# ---------------------------------------------------------------------------
# Default fetcher (network). Tests inject a stub instead.
# ---------------------------------------------------------------------------


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
}


def _default_fetcher(url: str, *, timeout: float = 8.0) -> FetchOutcome:
    """Fetch *url* once (GET) so liveness AND page text are available."""
    try:
        resp = requests.get(
            url,
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
        ok = 200 <= resp.status_code < 300
        text = (resp.text or "") if ok else ""
        return FetchOutcome(
            ok=ok, status_code=resp.status_code, page_text=text[:50_000]
        )
    except requests.RequestException:
        return FetchOutcome(ok=False, status_code=0, page_text="")


# ---------------------------------------------------------------------------
# Pure helpers (all deterministic, no I/O)
# ---------------------------------------------------------------------------


def _normalize_host(website: str) -> str | None:
    """Return a validated, lowercased host, or None if the URL is malformed.

    Malformed includes: non-web schemes, empty host, invalid host characters,
    and no dot in the host (bare ``localhost``-style values).
    """
    url = normalize_url(website)
    if url is None:
        return None
    host = extract_host(url)
    if not host:
        return None
    host = host.lower().rstrip(".")
    if "." not in host or not _VALID_HOST_RE.match(host):
        return None
    return host


def _is_placeholder_or_reserved(host: str) -> bool:
    if host in _PLACEHOLDER_HOSTS:
        return True
    if any(host.endswith(sfx) for sfx in _RESERVED_SUFFIXES):
        return True
    if _PLACEHOLDER_TOKEN_RE.search(host):
        return True
    return False


def _is_aggregator(host: str) -> bool:
    """Aggregator / directory / non-commercial host -> not an official site."""
    return host in _AGGREGATOR_HOSTS or _is_blocked_domain(f"https://{host}/")


def _has_directory_path(path: str) -> bool:
    segments = {s for s in path.split("/") if s}
    return bool(segments & _DIRECTORY_PATH_TOKENS)


def _name_tokens(company_name: str) -> list[str]:
    """Significant tokens from a company name (drops corporate suffixes)."""
    raw = re.split(r"[^A-Za-z0-9]+", company_name.lower())
    return [t for t in raw if len(t) >= 3 and t not in _CORPORATE_SUFFIXES]


_DOMAIN_STOP = frozenset({"www", "the", "my", "best", "usa", "us", "texas", "tx"})


def _domain_labels(host: str) -> list[str]:
    """Host labels minus the TLD (last label) and generic stopwords."""
    parts = host.split(".")
    return [p for p in parts[:-1] if p and p not in _DOMAIN_STOP]


def _domain_overlap(company_name: str, host: str) -> float:
    """Fraction of significant name tokens present in the domain labels.

    ``atlasroofing.com`` for "Atlas Roofing LLC" -> 1.0 (atlas + roofing both
    appear as substrings of the single label). ``bestshingles.com`` for the
    same name -> 0.0.
    """
    name_toks = _name_tokens(company_name)
    if not name_toks:
        return 0.0
    labels = _domain_labels(host)
    matched = sum(1 for tok in name_toks if any(tok in label for label in labels))
    return matched / len(name_toks)


def _name_on_page(
    company_name: str, name_tokens: list[str], page_text: str
) -> bool:
    """True when the company name appears on the page.

    A distinctive (non-generic-trade) token such as ``atlas`` is enough;
    generic words like ``roofing`` alone are not.
    """
    if not page_text:
        return False
    text = page_text.lower()
    full = (company_name or "").strip().lower()
    if len(full) >= 3 and full in text:
        return True
    distinctive = [t for t in name_tokens if t not in _GENERIC_TRADE_WORDS]
    candidates = distinctive or name_tokens
    return any(tok in text for tok in candidates)


def _compute_confidence(
    *,
    official_site_confirmed: bool,
    overlap: float,
    name_verified: bool,
    tier: SourceTier,
) -> float:
    """Evidence-based confidence (0..1). Syntax alone is never enough.

    - confirmed official site: 0.5 base + up to 0.4 overlap + 0.1 name;
    - name corroborated by a Tier 1/2 source (no site): 0.3;
    - anything else: 0.0.
    """
    if official_site_confirmed:
        conf = 0.5 + 0.4 * overlap + (0.1 if name_verified else 0.0)
    elif name_verified and tier in (SourceTier.OFFICIAL, SourceTier.TRUSTED):
        conf = 0.3
    else:
        conf = 0.0
    return round(min(conf, 1.0), 2)


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------


class IdentityVerifier:
    """Deterministic identity + official-website verification.

    ``fetcher`` is injectable so tests run fully offline. The default issues
    a single GET (liveness + page text).
    """

    def __init__(
        self,
        *,
        fetcher: Callable[[str], FetchOutcome] | None = None,
        timeout: float = 8.0,
    ) -> None:
        self._fetcher = fetcher or (lambda url: _default_fetcher(url, timeout=timeout))

    def verify(
        self,
        *,
        company_name: str = "",
        website: str = "",
        source: str = "",
        source_url: str = "",
        source_type: str | None = None,
    ) -> IdentityVerificationResult:
        """Verify identity from SUPPLIED evidence only (never query context).

        Parameters:
            company_name: the discovered company name.
            website: the candidate official website URL.
            source / source_url: provenance of the record (e.g. a Tier 1/2
                licensing board corroborates the name even without a website).
            source_type: optional source kind for tier mapping
                (see ``source_tiers.tier_for_source``).

        Returns:
            An ``IdentityVerificationResult``; this method never raises.
        """
        reasons: list[str] = []
        evidence: list[FieldEvidence] = []
        tier = tier_for_source(source, source_type)

        name_tokens = _name_tokens(company_name)
        name_credible = bool(name_tokens)  # >=1 significant token => plausible name

        website_status = VerificationStatus.unknown
        official_site_confirmed = False
        normalized_website = ""
        overlap = 0.0
        name_on_page = False

        raw_website = (website or "").strip()
        if raw_website:
            host = _normalize_host(raw_website)
            if host is None:
                website_status = VerificationStatus.rejected
                reasons.append("malformed URL: not a usable web address")
            elif _is_placeholder_or_reserved(host):
                website_status = VerificationStatus.rejected
                reasons.append(f"placeholder/reserved URL: {host}")
            elif _is_aggregator(host):
                website_status = VerificationStatus.rejected
                reasons.append(
                    f"aggregator/directory or non-commercial host, "
                    f"not an official website: {host}"
                )
            else:
                normalized_website = normalize_url(raw_website) or ""
                path = urlparse(normalized_website).path or "/"
                overlap = _domain_overlap(company_name, host)

                if _has_directory_path(path) and overlap < _OVERLAP_MIN:
                    website_status = VerificationStatus.rejected
                    reasons.append(
                        "directory member page (path), not an official website"
                    )
                else:
                    outcome = self._fetcher(normalized_website)
                    if not outcome.ok:
                        website_status = VerificationStatus.unknown
                        reasons.append(
                            f"website unreachable (HTTP {outcome.status_code}) "
                            f"— not verified, company not judged fake"
                        )
                    else:
                        name_on_page = _name_on_page(
                            company_name, name_tokens, outcome.page_text
                        )
                        if overlap >= _OVERLAP_MIN and name_on_page:
                            official_site_confirmed = True
                            website_status = VerificationStatus.verified
                            reasons.append(
                                "official website confirmed: reachable, "
                                "domain matches name, name on page"
                            )
                        else:
                            website_status = VerificationStatus.unknown
                            reasons.append(
                                "website reachable but no ownership evidence "
                                "(domain/name mismatch or company name absent)"
                            )
        else:
            reasons.append("no website supplied")

        # Name identity: corroborated by the confirmed official site OR by a
        # Tier 1/2 source (government / licensing / trusted directory).
        name_verified = False
        if name_credible and (
            official_site_confirmed
            or tier in (SourceTier.OFFICIAL, SourceTier.TRUSTED)
        ):
            name_verified = True
            if official_site_confirmed:
                reasons.append("company name corroborated by its official website")
            else:
                reasons.append(f"company name corroborated by Tier {int(tier)} source")

        if official_site_confirmed and name_verified:
            identity_status = VerificationStatus.verified
        elif official_site_confirmed or name_verified:
            identity_status = VerificationStatus.partially_verified
        else:
            identity_status = VerificationStatus.unknown

        confidence = _compute_confidence(
            official_site_confirmed=official_site_confirmed,
            overlap=overlap,
            name_verified=name_verified,
            tier=tier,
        )

        # Evidence trail (provenance). Rejected/unknown values are recorded
        # with is_reliable=False rather than being silently dropped.
        if raw_website:
            evidence.append(
                FieldEvidence(
                    field="website",
                    value=normalized_website or raw_website,
                    source=source or "identity_verifier",
                    source_url=source_url,
                    confidence=1.0
                    if website_status == VerificationStatus.verified
                    else 0.0,
                    is_reliable=website_status == VerificationStatus.verified,
                )
            )
        # INPUT DATA IS NOT EVIDENCE: a bare company_name is a candidate
        # value, not proof. Only record it as FieldEvidence when an actual
        # source corroborates it (confirmed official site or Tier 1/2 source).
        if name_verified and company_name and company_name.strip():
            evidence.append(
                FieldEvidence(
                    field="company_name",
                    value=company_name.strip(),
                    source=source or "identity_verifier",
                    source_url=source_url,
                    confidence=1.0,
                    is_reliable=True,
                )
            )

        return IdentityVerificationResult(
            identity_status=identity_status,
            website_status=website_status,
            official_site_confirmed=official_site_confirmed,
            name_verified=name_verified,
            domain_overlap=round(overlap, 2),
            confidence=confidence,
            reasons=reasons,
            evidence=evidence,
            normalized_website=normalized_website,
        )
