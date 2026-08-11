"""Contractor classifier — filters construction companies from general web results.

Accepts: General Contractors, Roofing, Plumbing, Electrical, HVAC,
         Concrete, Painting, Flooring, Steel, Landscaping contractors.

Rejects: Manufacturers, Suppliers, Distributors, Product brands,
         Building material companies, Software companies, Directories,
         Government pages, News sites, Blogs.
"""

from __future__ import annotations

import re
from typing import Any

# Trades we accept
_ACCEPTED_TRADES: frozenset[str] = frozenset(
    {
        "general_contractor",
        "roofing",
        "plumbing",
        "electrical",
        "hvac",
        "concrete",
        "painting",
        "flooring",
        "steel",
        "landscaping",
    }
)

# Keywords that indicate a non-contractor business
_REJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bmanufacturer\b", re.IGNORECASE), "manufacturer"),
    (re.compile(r"\bsupplier\b", re.IGNORECASE), "supplier"),
    (re.compile(r"\bdistributor\b", re.IGNORECASE), "distributor"),
    (re.compile(r"\bwholesaler\b", re.IGNORECASE), "wholesaler"),
    (
        re.compile(r"\bbuilding\s+materials?\b", re.IGNORECASE),
        "building materials company",
    ),
    (re.compile(r"\bmaterial\s+company\b", re.IGNORECASE), "material company"),
    (re.compile(r"\bsoftware\b", re.IGNORECASE), "software company"),
    (re.compile(r"\bsaas\b", re.IGNORECASE), "SaaS company"),
    (re.compile(r"\bapp\s+developer\b", re.IGNORECASE), "software company"),
    (re.compile(r"\bdirectory\b", re.IGNORECASE), "directory site"),
    (re.compile(r"\blog\s+(spot|house|page)?\b", re.IGNORECASE), "blog"),
    (re.compile(r"\bnews\s+(site|outlet|network)?\b", re.IGNORECASE), "news site"),
    (re.compile(r"\bgovernment\b|\.gov\b", re.IGNORECASE), "government entity"),
    (re.compile(r"\beducation\b|\.edu\b", re.IGNORECASE), "educational institution"),
    (re.compile(r"\bnonprofit\b|\.org\b", re.IGNORECASE), "nonprofit organization"),
    (re.compile(r"\bforum\b", re.IGNORECASE), "forum"),
    (re.compile(r"\bwiki\b", re.IGNORECASE), "wiki"),
    (re.compile(r"\breview\s+(site|company)\b", re.IGNORECASE), "review site"),
    (
        re.compile(r"\b(?:best|top|rated)\s+\w*\s*reviews?\b", re.IGNORECASE),
        "review site",
    ),
    (re.compile(r"\brating\s+service\b", re.IGNORECASE), "rating service"),
    (re.compile(r"\be-commerce\b|ecommerce\b", re.IGNORECASE), "e-commerce site"),
    (re.compile(r"\bonline\s+store\b", re.IGNORECASE), "online store"),
    (re.compile(r"\bmarketplace\b", re.IGNORECASE), "marketplace"),
]

# Trade classification patterns
_TRADE_PATTERNS: dict[str, re.Pattern] = {
    "general_contractor": re.compile(
        r"\b(general\s+contractor|gc|contracting|construction\s+company"
        r"|commercial\s+contractor|building\s+contractor|remodeler|renovator)"
        r"\b",
        re.IGNORECASE,
    ),
    "roofing": re.compile(
        r"\b(roof|roofing|shingle|tpo|epdm|metal\s+roof|gutter|roofing\s+contractor)"
        r"\b",
        re.IGNORECASE,
    ),
    "plumbing": re.compile(
        r"\b(plumb|pipe|plumber|drain|sewer|water\s+heater|hvac|mechanical)" r"\b",
        re.IGNORECASE,
    ),
    "electrical": re.compile(
        r"\b(electric|electrical|wiring|breaker|panel|generator|lighting|alarm)" r"\b",
        re.IGNORECASE,
    ),
    "hvac": re.compile(
        r"\b(hvac|heating|cooling|air\s+condition|furnace|boiler|ventilation)" r"\b",
        re.IGNORECASE,
    ),
    "concrete": re.compile(
        r"\b(concrete|cement|foundation|slab|paving|driveway|flatwork)" r"\b",
        re.IGNORECASE,
    ),
    "painting": re.compile(
        r"\b(paint|painting|coating|staining|drywall)" r"\b",
        re.IGNORECASE,
    ),
    "flooring": re.compile(
        r"\b(floor|flooring|carpet|tile|hardwood|laminate|vinyl)" r"\b",
        re.IGNORECASE,
    ),
    "steel": re.compile(
        r"\b(steel|structural|metal\s+fabrication|welding|iron\s+work|erection)" r"\b",
        re.IGNORECASE,
    ),
    "landscaping": re.compile(
        r"\b(landscape|landscaping|lawn|gardening|irrigation|sprinkler|hardscape)"
        r"\b",
        re.IGNORECASE,
    ),
}


class ContractorClassifier:
    """Classifies web results as construction contractors or rejects them.

    The classifier uses keyword matching against company names, titles,
    and descriptions to determine if a result is a legitimate construction
    contractor or a non-contractor entity.
    """

    def classify(
        self,
        *,
        name: str = "",
        title: str = "",
        description: str = "",
        url: str = "",
        industry_hint: str = "",
    ) -> dict[str, Any]:
        """Classify a potential company result.

        Args:
            name: Company name.
            title: Page title.
            description: Page description.
            url: Company website URL.
            industry_hint: Known industry hint from search context.

        Returns:
            Dict with 'accepted' (bool), 'trade_category' (str),
            'reject_reason' (str or None).
        """
        # The page's own content (name/title/description), not the query.
        # The industry hint is the SEARCH context, not evidence a page is
        # a contractor: it may confirm a trade the page already states,
        # but it must never manufacture acceptance from nothing (see the
        # hint block below). Otherwise any page crawled under a "Roofing"
        # query — a login page, an association homepage — would be accepted
        # as a roofing contractor (CLAUDE.md §7).
        text = f"{name} {title} {description}".lower()

        # Check for rejection keywords first
        for pattern, reason in _REJECT_PATTERNS:
            if pattern.search(text):
                return {
                    "accepted": False,
                    "trade_category": "",
                    "reject_reason": reason,
                }

        # Check domain patterns for obvious non-contractors
        blocked_domains = [
            "wikipedia.org",
            ".gov",
            ".edu",
            "linkedin.com/in/",
            "facebook.com/",
            "twitter.com/",
            "instagram.com/",
            "indeed.com",
            "monster.com",
            "angieslist.com",
            "yelp.com",
            "yellowpages.com",
            "houzz.com",
        ]
        url_lower = url.lower()
        for domain in blocked_domains:
            if domain in url_lower:
                return {
                    "accepted": False,
                    "trade_category": "",
                    "reject_reason": f"blocked domain: {domain}",
                }

        # Classify trade category
        trade_score: dict[str, int] = {}
        for trade, pattern in _TRADE_PATTERNS.items():
            matches = pattern.findall(text)
            if matches:
                trade_score[trade] = len(matches)

        # The industry hint only disambiguates a trade the page's own
        # content already matched — never creates one. A real roofing
        # contractor's page states its trade; a "Member Login" page does
        # not, so no amount of hint should turn it into a roofing company.
        if industry_hint:
            hint_lower = industry_hint.lower()
            for trade, pattern in _TRADE_PATTERNS.items():
                if trade in trade_score and pattern.search(hint_lower):
                    trade_score[trade] += 5

        if not trade_score:
            return {
                "accepted": False,
                "trade_category": "",
                "reject_reason": "no construction trade match",
            }

        best_trade = max(trade_score, key=trade_score.get)
        confidence = min(trade_score[best_trade] / 3.0, 1.0)

        return {
            "accepted": confidence >= 0.3,
            "trade_category": best_trade,
            "reject_reason": None,
            "confidence": confidence,
        }
