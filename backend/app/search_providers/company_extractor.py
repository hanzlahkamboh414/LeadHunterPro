"""Company extraction from crawled web pages.

Extracts structured company information from HTML pages using
regex patterns, meta tags, and semantic heuristics. Designed to
work with the existing HTMLParser infrastructure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.email.email_cleaner import is_crawl_artifact

logger = logging.getLogger(__name__)

# Patterns for identifying business entities
_PHONE_PATTERN = re.compile(
    r"(?:tel:|phone:)?\s*(?:\+?1[-.\s]?)?" r"(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}",
    re.IGNORECASE,
)

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_ADDRESS_PATTERN = re.compile(
    r"\d+\s+[A-Za-z]+(?:\s+[A-Za-z]+)*," r"\s*[A-Za-z\s]+,\s*[A-Z]{2}\s*\d{5}",
    re.IGNORECASE,
)

# "City, ST" with a mandatory comma and an UPPERCASE state code, and no
# case-insensitivity. The loose variant (optional comma, IGNORECASE)
# matched HTML noise — e.g. "chro, ME" from "through ... member" — so a
# state is now only accepted when it looks like an address: a Title-case
# city immediately followed by ", " and a two-letter uppercase code.
_CTY_STATE_PATTERN = re.compile(
    r"(?<![A-Za-z])([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*,\s*([A-Z]{2})(?![A-Za-z])"
)

# --- Company-name sources (roadmap D19) --------------------------------------
#
# Name sources are consulted STRUCTURED-FIRST. The previous order tried
# ``og:title`` first and called it "most reliable"; the 2026-08-19 live run
# disproved that — every discovered company came back named with its SEO page
# headline ("Dallas Roofing Contractor Since 1983 | Arrington Roofing").
# ``og:title`` is copy written for search engines; schema.org and ``og:site_name``
# are the only fields that carry a *business* name by definition, so they now win
# whenever a site publishes them. A wrong name is not cosmetic: it is the join key
# for downstream enrichment and it is what the customer receives in the export.

# Business schema types small contractors actually publish. Matching only
# "Organization" was a real gap — trade sites overwhelmingly emit LocalBusiness or
# one of its subtypes (RoofingContractor, HVACBusiness, Plumber,
# GeneralContractor, HomeAndConstructionBusiness).
_SCHEMA_BUSINESS_TYPE = (
    r"(?:Organization|Corporation|LocalBusiness"
    r"|[A-Za-z]*(?:Business|Contractor|Plumber|Electrician|Roofing))"
)

# "name" may appear before or after "@type" inside the same JSON-LD object, so
# both orders are tried. Excluding braces keeps each match inside one object,
# preventing a name from being harvested out of a *neighbouring* node.
_SCHEMA_NAME_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(
        r'"@type"\s*:\s*"' + _SCHEMA_BUSINESS_TYPE + r'"[^{}]*?"name"\s*:\s*"([^"]+)"',
        re.IGNORECASE,
    ),
    re.compile(
        r'"name"\s*:\s*"([^"]+)"[^{}]*?"@type"\s*:\s*"' + _SCHEMA_BUSINESS_TYPE + r'"',
        re.IGNORECASE,
    ),
)

# Separators used to break an SEO title into segments. A bare "-" only counts
# when surrounded by whitespace, so hyphenated names ("Tri-State Roofing") are
# never split in half.
_TITLE_SPLIT_PATTERN = re.compile(r"\s*[|–—·»]\s*|\s+-\s+")

# Unambiguously promotional vocabulary. Used ONLY to rank one title segment
# against another when the domain cannot settle it — never to reject a name,
# because a real company may legitimately be "Premier Roofing" or "Quality Roof
# Co", and rejecting those would trade one wrong-name bug for another.
_PROMO_TOKENS: frozenset[str] = frozenset(
    {
        "affordable",
        "award",
        "awards",
        "best",
        "bonded",
        "cheap",
        "certified",
        "companies",
        "contractors",
        "estimate",
        "estimates",
        "expert",
        "experts",
        "free",
        "guaranteed",
        "insured",
        "leading",
        "licensed",
        "me",
        "near",
        "no",
        "number",
        "official",
        "operated",
        "owned",
        "premier",
        "professional",
        "quote",
        "quotes",
        "rated",
        "reliable",
        "review",
        "reviews",
        "since",
        "top",
        "trusted",
        "vetted",
        "voted",
        "welcome",
        "winning",
    }
)


def _norm_alnum(text: str) -> str:
    """Reduce text to lowercase alphanumerics so it can be compared to a domain.

    Args:
        text: Any string.

    Returns:
        The input lowercased with every non-alphanumeric character removed
        ("Arrington Roofing, Inc." -> "arringtonroofinginc").
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _meta_content(html: str, key: str) -> str:
    """Read a ``<meta>`` tag's content by its ``property`` or ``name`` key.

    Both attribute orders are handled, because real pages emit
    ``property=... content=...`` and ``content=... property=...`` alike, and a
    pattern that assumes one order silently loses the tag on half the web. The
    quote character is captured and back-referenced rather than matched as a
    class, so an apostrophe inside a double-quoted value does not truncate it
    ("Joe's Roofing Co" must not become "Joe"). The ``[^>]*?`` between the two
    attributes keeps each match inside a single tag.

    Args:
        html: Raw HTML content.
        key: The meta key to look up, e.g. ``"og:site_name"``.

    Returns:
        The tag's content value, or an empty string when the tag is absent.
    """
    escaped = re.escape(key)
    key_attr = rf'(?:property|name)\s*=\s*(?P<kq>["\']){escaped}(?P=kq)'
    value_attr = r'content\s*=\s*(?P<vq>["\'])(?P<value>.*?)(?P=vq)'
    patterns = (
        rf"<meta[^>]+?{key_attr}[^>]*?{value_attr}",
        rf"<meta[^>]+?{value_attr}[^>]*?{key_attr}",
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            return match.group("value").strip()
    return ""


def _domain_label(url: str) -> str:
    """Reduce a URL to the comparable label of its host.

    Args:
        url: Absolute page URL. May be empty.

    Returns:
        The lowercased alphanumeric first label ("arringtonroofing" for
        ``https://www.arringtonroofing.com/about``), or an empty string when the
        URL is missing or has no host.
    """
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    host = host.removeprefix("www.")
    return _norm_alnum(host.split(".")[0])


def _promo_score(segment: str) -> int:
    """Count advertising signals in one title segment; lower is more name-like.

    Args:
        segment: A single title segment.

    Returns:
        A non-negative score. Only used for ranking segments against each other.
    """
    words = re.findall(r"[a-z0-9]+", segment.lower())
    score = sum(1 for word in words if word in _PROMO_TOKENS)
    # "#1 Roofer in Dallas" and a bare year are pure marketing, and neither is
    # caught by the token list.
    if segment.lstrip().startswith("#"):
        score += 2
    score += sum(1 for word in words if re.fullmatch(r"(?:19|20)\d{2}", word))
    return score


def _best_name_segment(text: str, domain: str) -> str:
    """Pick the segment of a title most likely to be the business name.

    Args:
        text: A title-like string, possibly "Tagline | Brand | City".
        domain: Normalized domain label from :func:`_domain_label`. May be empty.

    Returns:
        The chosen segment, or ``text`` stripped when it has no separators.
    """
    segments = [s.strip() for s in _TITLE_SPLIT_PATTERN.split(text) if s.strip()]
    if len(segments) < 2:
        return text.strip()

    # A segment echoing the domain is decisive: small businesses register their
    # own name (Arrington Roofing -> arringtonroofing.com), so this is corroboration
    # from a second independent source rather than a guess about which words look
    # promotional. The length floor keeps a stray "TX" from matching by accident.
    if domain:
        for segment in segments:
            norm = _norm_alnum(segment)
            if len(norm) < 4:
                continue
            if norm == domain:
                return segment
            overlap = norm in domain or domain in norm
            if overlap and min(len(norm), len(domain)) >= 0.6 * max(
                len(norm), len(domain)
            ):
                return segment

    # No domain corroboration: prefer the segment that reads least like an
    # advertisement, breaking ties on brevity, since brands are short and
    # taglines are long.
    return min(
        segments, key=lambda s: (_promo_score(s), len(s.split()), len(s), s.lower())
    )


# Keywords that indicate non-contractor businesses (to reject)
_REJECT_KEYWORDS: frozenset[str] = frozenset(
    {
        "manufacturer",
        "supplier",
        "distributor",
        "wholesaler",
        "brand",
        "product",
        "software",
        "app",
        "saas",
        "platform",
        "blog",
        "news",
        "directory",
        "portal",
        "forum",
        "wiki",
        "government",
        "gov",
        "agency",
        "department",
        "municipality",
        "nonprofit",
        "charity",
        "foundation",
        "association",
        "university",
        "college",
        "school",
        "educational",
        "ecommerce",
        "store",
        "shop",
        "retail",
        "marketplace",
        "review",
        "rating",
        "comparison",
        "listicle",
        "portfolio",
        "freelance",
        "consultant",
        "individual",
    }
)

# Trade-specific keywords for classification
_TRADE_KEYWORDS: dict[str, list[str]] = {
    "general_contractor": [
        "general contractor",
        "general contractors",
        "gc",
        "contracting",
        "construction company",
        "construction companies",
        "commercial contractor",
        "building contractor",
        "remodeling",
        "renovation",
        "home builder",
        "home builders",
    ],
    "roofing": [
        "roof",
        "roofing",
        "shingle",
        "shingles",
        "tpo",
        "epdm",
        "gutter",
        "gutters",
        "metal roof",
        "roof repair",
        "roof replacement",
        "roof installation",
    ],
    "plumbing": [
        "plumb",
        "pipe",
        "plumber",
        "drain",
        "sewer",
        "water heater",
        "hvac",
        "heating",
        "cooling",
        "air conditioning",
        "duct",
        "ventilation",
        "mechanical",
    ],
    "electrical": [
        "electric",
        "electrical",
        "wiring",
        "breaker",
        "panel",
        "generator",
        "lighting",
        "alarm",
        "security system",
    ],
    "hvac": [
        "hvac",
        "heating",
        "cooling",
        "air conditioning",
        "air con",
        "ac",
        "furnace",
        "boiler",
        "ventilation",
        "thermostat",
        "ductwork",
    ],
    "concrete": [
        "concrete",
        "cement",
        "foundation",
        "slab",
        "paving",
        "driveway",
        "sidewalk",
        "flatwork",
    ],
    "painting": [
        "paint",
        "painting",
        "coating",
        "staining",
        "drywall",
        "interior paint",
        "exterior paint",
    ],
    "flooring": [
        "floor",
        "flooring",
        "carpet",
        "tile",
        "hardwood",
        "laminate",
        "vinyl",
        "installation",
    ],
    "steel": [
        "steel",
        "structural",
        "metal fabrication",
        "welding",
        "iron work",
        "beam",
        "column",
        "erection",
    ],
    "landscaping": [
        "landscape",
        "landscaping",
        "lawn",
        "gardening",
        "irrigation",
        "sprinkler",
        "outdoor",
        "hardscape",
    ],
}


@dataclass
class CompanyProfile:
    """Extracted company profile from a website.

    Attributes:
        name: Company legal or trade name.
        website: Canonical homepage URL.
        city: City from address or page content.
        state: State code (2-letter).
        country: Country code (default "USA").
        trade_category: Classified trade category.
        industry_focus: Descriptive industry focus text.
        phone: Primary phone number found.
        email: Primary email found.
        address: Physical address if found.
        source_url: URL where this info was extracted from.
        confidence: Extraction confidence (0.0-1.0).
        rejected_reason: Why this was rejected, if applicable.
    """

    name: str = ""
    website: str = ""
    city: str = ""
    state: str = ""
    country: str = "USA"
    trade_category: str = ""
    industry_focus: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    source_url: str = ""
    confidence: float = 0.0
    rejected_reason: str = ""

    @property
    def is_rejected(self) -> bool:
        """True if this company was rejected during classification."""
        return bool(self.rejected_reason)

    @property
    def is_valid(self) -> bool:
        """True if this company has enough data to be useful."""
        return bool(self.name and self.website and not self.is_rejected)


class CompanyExtractor:
    """Extracts company information from crawled web pages.

    Uses regex patterns, meta tags, and semantic analysis to
    extract structured data from HTML content.
    """

    def __init__(self) -> None:
        """Initialize the extractor."""
        self._trade_patterns: dict[str, re.Pattern] = {
            trade: re.compile(
                r"\b" + "|".join(re.escape(kw) for kw in keywords) + r"\b",
                re.IGNORECASE,
            )
            for trade, keywords in _TRADE_KEYWORDS.items()
        }
        self._reject_pattern = re.compile(
            r"\b(" + "|".join(re.escape(kw) for kw in _REJECT_KEYWORDS) + r")\b",
            re.IGNORECASE,
        )

    def extract(
        self,
        url: str,
        html: str,
        *,
        title: str = "",
        description: str = "",
        context: dict[str, str] | None = None,
    ) -> CompanyProfile:
        """Extract company profile from HTML content.

        Args:
            url: The page URL being extracted from.
            html: Raw HTML content.
            title: Page title (pre-extracted).
            description: Meta description (pre-extracted).
            context: Additional context hints
                ({industry_hint, location_hint}).

        Returns:
            CompanyProfile with extracted data.
        """
        profile = CompanyProfile(
            website=url,
            source_url=url,
            name=self._extract_name(html, title, url=url),
            industry_focus=self._extract_industry_focus(html, description),
        )

        # Extract contact info
        self._extract_contact(profile, html)

        # Extract location
        self._extract_location(profile, html)

        # Classify trade
        profile.trade_category = self._classify_trade(
            profile.industry_focus,
            title=title,
            context=context,
        )

        # Check for rejection
        profile.rejected_reason = self._check_rejection(
            profile, html, title, description
        )

        # Calculate confidence
        profile.confidence = self._calc_confidence(profile)

        logger.debug(
            "Extracted company from %s: name=%r trade=%r conf=%.2f reject=%r",
            url,
            profile.name[:30],
            profile.trade_category,
            profile.confidence,
            profile.rejected_reason,
        )
        return profile

    def _extract_name(self, html: str, title: str, url: str = "") -> str:
        """Extract the company's business name from a page.

        Sources are consulted structured-first — schema.org, then ``og:site_name``,
        and only then the marketing titles — because only the first two carry a
        business name by definition. See roadmap D19 for the live evidence that
        the previous ``og:title``-first order returned SEO headlines instead of
        company names.

        Args:
            html: Raw HTML content.
            title: Page title (pre-extracted).
            url: Page URL. Optional; used only to recognise which segment of a
                multi-part title matches the site's own domain. Extraction still
                works without it, which is why direct callers may omit it.

        Returns:
            Extracted company name, or an empty string when the page carries
            nothing usable.
        """
        # 1. schema.org — a machine-readable business name, published by the site
        #    owner for exactly this purpose.
        for pattern in _SCHEMA_NAME_PATTERNS:
            match = pattern.search(html)
            if match and match.group(1).strip():
                return match.group(1).strip()

        # 2. og:site_name — the OpenGraph field meaning "the name of this site",
        #    as opposed to og:title which means "the headline of this page". It
        #    was previously not consulted at all.
        site_name = _meta_content(html, "og:site_name")
        if site_name:
            return site_name

        # 3./4. og:title, then <title>. Both are marketing strings, so they are
        #    accepted only after being reduced to their most name-like segment.
        domain = _domain_label(url)
        for candidate in (_meta_content(html, "og:title"), title):
            if not candidate:
                continue
            name = _best_name_segment(candidate, domain)
            # Strip the search-engine and page-role suffixes the previous
            # implementation removed, so this path never regresses on pages
            # where it already produced a usable name.
            name = re.sub(
                r"\s*[-|—]\s*(Google|Bing|Yahoo|Facebook)",
                "",
                name,
                flags=re.IGNORECASE,
            )
            name = re.sub(
                r"\s*[-|—]\s*(Contact|About|Home)", "", name, flags=re.IGNORECASE
            ).strip()
            if len(name) > 2:
                return name

        return ""

    def _extract_industry_focus(self, html: str, description: str) -> str:
        """Extract industry focus description.

        Args:
            html: Raw HTML content.
            description: Meta description.

        Returns:
            Industry focus text.
        """
        # Use meta description if available
        if description:
            return description.strip()

        # Extract from H1 and lead paragraph
        h1_match = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
        if h1_match:
            return h1_match.group(1).strip()

        # Get first substantial paragraph
        para_matches = re.findall(r"<p[^>]*>([^<]{50,})</p>", html, re.IGNORECASE)
        if para_matches:
            return para_matches[0].strip()

        return ""

    def _extract_contact(self, profile: CompanyProfile, html: str) -> None:
        """Extract phone and email from HTML.

        Args:
            profile: The company profile to populate.
            html: Raw HTML content.
        """
        # Phone
        phones = _PHONE_PATTERN.findall(html)
        if phones:
            # Clean and pick the most complete one
            cleaned = []
            for p in phones:
                clean = re.sub(r"[^0-9+\-().\s]", "", p)
                digits = re.sub(r"\D", "", clean)
                if len(digits) >= 7:
                    cleaned.append(clean)
            if cleaned:
                profile.phone = cleaned[0]

        # Email
        emails = _EMAIL_PATTERN.findall(html)
        # Crawl artifacts first (Sentry DSNs, example.com placeholders,
        # mailing-list ids — machine strings the regex cannot tell apart
        # from contacts on raw page source).
        emails = [e for e in emails if not is_crawl_artifact(e)]
        if emails:
            # Filter out obvious non-business emails
            business_emails = [
                e
                for e in emails
                if not any(x in e.lower() for x in ["gmail", "yahoo", "hotmail", "aol"])
            ]
            if business_emails:
                profile.email = business_emails[0]
            else:
                profile.email = emails[0]

    def _extract_location(self, profile: CompanyProfile, html: str) -> None:
        """Extract city and state from HTML.

        Args:
            profile: The company profile to populate.
            html: Raw HTML content.
        """
        # Try schema.org Address
        addr_match = re.search(
            r'"address"\s*:\s*\{[^}]*"addressLocality"\s*:\s*"([^"]+)"'
            r'[^}]*"addressRegion"\s*:\s*"([^"]+)"',
            html,
            re.IGNORECASE,
        )
        if addr_match:
            profile.city = addr_match.group(1).strip()
            profile.state = addr_match.group(2).strip().upper()
            return

        # Try text pattern
        loc_match = _CTY_STATE_PATTERN.search(html)
        if loc_match:
            city = loc_match.group(1).strip()
            state = loc_match.group(2).strip().upper()
            # Validate state code
            if state in (
                "AL",
                "AK",
                "AZ",
                "AR",
                "CA",
                "CO",
                "CT",
                "DE",
                "FL",
                "GA",
                "HI",
                "ID",
                "IL",
                "IN",
                "IA",
                "KS",
                "KY",
                "LA",
                "ME",
                "MD",
                "MA",
                "MI",
                "MN",
                "MS",
                "MO",
                "MT",
                "NE",
                "NV",
                "NH",
                "NJ",
                "NM",
                "NY",
                "NC",
                "ND",
                "OH",
                "OK",
                "OR",
                "PA",
                "RI",
                "SC",
                "SD",
                "TN",
                "TX",
                "UT",
                "VT",
                "VA",
                "WA",
                "WV",
                "WI",
                "WY",
            ):
                profile.city = city
                profile.state = state

    def _classify_trade(
        self,
        industry_focus: str,
        *,
        title: str = "",
        context: dict[str, str] | None = None,
    ) -> str:
        """Classify the company's trade category.

        Args:
            industry_focus: Extracted industry focus text.
            title: Page title for additional context.
            context: Extra hints ({industry_hint}).

        Returns:
            Trade category string, or empty if unclassified.
        """
        text = f"{title} {industry_focus}".lower()

        # Classify from the page's own content only. The industry hint is
        # the QUERY context, not page evidence — it may confirm a trade the
        # page already states, but it must never manufacture a trade from
        # nothing (a "Member Login" page must not become "roofing" just
        # because the query said "Roofing" — CLAUDE.md §7). Mirrors the
        # ContractorClassifier's hint handling.
        content_scores: dict[str, int] = {}
        for trade, pattern in self._trade_patterns.items():
            score = len(pattern.findall(text))
            if score:
                content_scores[trade] = score

        if context and context.get("industry_hint"):
            hint = context["industry_hint"].lower()
            for trade, pattern in self._trade_patterns.items():
                if trade in content_scores and pattern.search(hint):
                    content_scores[trade] += 5

        if not content_scores:
            return ""

        return max(content_scores, key=content_scores.get)

    def _check_rejection(
        self,
        profile: CompanyProfile,
        html: str,
        title: str,
        description: str,
    ) -> str:
        """Check if this company should be rejected.

        Args:
            profile: The extracted company profile.
            html: Raw HTML content.
            title: Page title.
            description: Meta description.

        Returns:
            Empty string if accepted, reason if rejected.
        """
        text_to_check = f"{title} {description} {profile.industry_focus}".lower()

        # Check for reject keywords
        reject_matches = self._reject_pattern.findall(text_to_check)
        if reject_matches:
            return f"Rejected: contains rejected keyword '{reject_matches[0]}'"

        # Must have a meaningful name
        if not profile.name or len(profile.name.strip()) < 3:
            return "Rejected: no valid company name"

        # Must be in construction trades
        if not profile.trade_category:
            return "Rejected: could not classify as construction trade"

        return ""

    def _calc_confidence(self, profile: CompanyProfile) -> float:
        """Calculate extraction confidence score.

        Args:
            profile: The extracted company profile.

        Returns:
            Confidence score between 0.0 and 1.0.
        """
        if profile.is_rejected:
            return 0.0

        score = 0.0
        max_score = 5.0

        if profile.name:
            score += 1.0
        if profile.phone:
            score += 1.0
        if profile.email:
            score += 0.5
        if profile.city and profile.state:
            score += 1.0
        elif profile.city or profile.state:
            score += 0.5
        if profile.trade_category:
            score += 1.0
        if profile.industry_focus and len(profile.industry_focus) > 20:
            score += 0.5

        return min(score / max_score, 1.0)
