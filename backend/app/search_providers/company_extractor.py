"""Company extraction from crawled web pages.

Extracts structured company information from HTML pages using
regex patterns, meta tags, and semantic heuristics. Designed to
work with the existing HTMLParser infrastructure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

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
            name=self._extract_name(html, title),
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

    def _extract_name(self, html: str, title: str) -> str:
        """Extract company name from HTML/title.

        Args:
            html: Raw HTML content.
            title: Page title.

        Returns:
            Extracted company name.
        """
        # Try OG:title first (most reliable)
        og_match = re.search(
            r'<meta\s+property="og:title"\s+content="([^"]+)"', html, re.IGNORECASE
        )
        if og_match:
            return og_match.group(1).strip()

        # Try schema.org Organization
        org_match = re.search(
            r'"@type"\s*:\s*"Organization"[^}]*"name"\s*:\s*"([^"]+)"',
            html,
            re.IGNORECASE,
        )
        if org_match:
            return org_match.group(1).strip()

        # Fall back to <title> tag
        if title:
            # Remove common suffixes
            name = re.sub(
                r"\s*[-|—]\s*(Google|Bing|Yahoo|Facebook)",
                "",
                title,
                flags=re.IGNORECASE,
            )
            name = re.sub(
                r"\s*[-|—]\s*(Contact|About|Home)", "", name, flags=re.IGNORECASE
            )
            name = name.strip()
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
