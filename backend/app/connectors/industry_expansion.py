"""Industry keyword expansion for construction trade discovery.

Maps broad industry search terms to comprehensive keyword sets that
cover trade-specific terminology, materials, services, and related
professional titles. This enables connectors to match companies
whose listed focus areas may use different but equivalent terminology.

Example: A search for "Roofing" expands to include "roof repair",
"TPO", "EPDM", "shingle", "gutter", etc. — ensuring companies that
list themselves as "roof systems" or "commercial roofing" are found.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Primary industry-to-keyword mapping.
# Keys are canonical search terms (lowercase).
# Values are lists of expanded keywords and phrases to search within
# company_name, industry_focus, and description fields.
INDUSTRY_EXPANSION: dict[str, list[str]] = {
    "roofing": [
        "roof",
        "roofing",
        "roofer",
        "roof systems",
        "roof repair",
        "roof replacement",
        "commercial roofing",
        "industrial roofing",
        "metal roofing",
        "residential roofing",
        "roof contractor",
        "TPO",
        "EPDM",
        "modified bitumen",
        "shingle",
        "shingles",
        "gutter",
        "gutters",
        "gutter repair",
        "flat roof",
        "roof maintenance",
        "storm damage",
        "leak repair",
    ],
    "plumbing": [
        "plumbing",
        "plumber",
        "plumbing contractor",
        "pipe",
        "piping",
        "drain",
        "drainage",
        "sewer",
        "sewage",
        "water heater",
        "hot water",
        "mechanical plumbing",
        "emergency plumbing",
        "pipe repair",
        "gas line",
        "sump pump",
        "backflow",
        "water supply",
        " wastewater",
        "hydro-jetting",
    ],
    "electrical": [
        "electrical",
        "electrician",
        "electrical contractor",
        "electric",
        "power",
        "lighting",
        "generator",
        "panel",
        "panels",
        "controls",
        "low voltage",
        "substation",
        "wiring",
        "electrical systems",
        "rewiring",
        "outlet",
        "switch",
        "commercial electrical",
        "industrial electrical",
        "emergency electrical",
        "backup power",
    ],
    "hvac": [
        "hvac",
        "heating",
        "cooling",
        "air conditioning",
        "aircond",
        "furnace",
        "ventilation",
        "duct",
        "ductwork",
        "thermostat",
        "climate control",
        "heat pump",
        "boiler",
        "boilers",
        "air quality",
        "indoor air",
        "minisplit",
        "central air",
        "hvac contractor",
        "mechanical contractor",
    ],
    "general_contractor": [
        "general contractor",
        "gc",
        "contracting",
        "construction",
        "builder",
        "builders",
        "building contractor",
        "commercial builder",
        "residential builder",
        "home builder",
        "remodeling",
        "renovation",
        "remodel",
        "reno",
        "development",
        "developer",
        "demolition",
        "project management",
        "construction management",
    ],
    "commercial_construction": [
        "commercial construction",
        "commercial builder",
        "commercial builders",
        "construction estimating",
        "estimating",
        "cost consulting",
        "project management",
        "construction management",
        "heavy construction",
        "civil construction",
    ],
    "heavy_civil": [
        "heavy civil",
        "civil engineering",
        "infrastructure",
        "highways",
        "roads",
        "bridges",
        "paving",
        "excavation",
        "earthwork",
        "site work",
        "land development",
        "utilities",
        "stormwater",
        "drainage",
        "retaining wall",
    ],
    "residential": [
        "residential construction",
        "residential builder",
        "home builder",
        "home builders",
        "residential remodeling",
        "custom homes",
        "custom home builder",
        "new home",
        "home renovation",
        "home improvement",
    ],
    "concrete": [
        "concrete",
        "concrete contractor",
        "paving",
        "driveway",
        "foundation",
        "foundations",
        "slab",
        "slabs",
        "stamped concrete",
        "concite work",
        "flatwork",
        "retaining wall",
        "curb",
    ],
    "flooring": [
        "flooring",
        "carpet",
        "tile floor",
        "hardwood",
        "floor installation",
        "vinyl",
        "laminate",
        "linoleum",
        "epoxy floor",
        "polished concrete",
        "floor covering",
        "floor refinish",
    ],
    "painting": [
        "painting",
        "paint",
        "staining",
        "paint contractor",
        "interior painting",
        "exterior painting",
        "commercial painting",
        "residential painting",
        "decorative paint",
        "wallcovering",
    ],
    "landscaping": [
        "landscaping",
        "landscape",
        "lawn",
        "irrigation",
        "hardscape",
        "hardscaping",
        "outdoor living",
        "patio",
        "deck",
        "fence",
        "fencing",
        "tree service",
        "arborist",
        "sprinkler",
    ],
    "steel": [
        "steel",
        "steel fabrication",
        "structural steel",
        "steel worker",
        "steel erector",
        "metal fabrication",
        "welding",
        "welder",
        "ironworker",
        "iron working",
        "metalwork",
    ],
    "masonry": [
        "masonry",
        "mason",
        "brick",
        "block",
        "stone",
        "stucco",
        "tile",
        "pavers",
        "fireplace",
        "chimney",
    ],
    "glass": [
        "glass",
        "glazing",
        "window",
        "windows",
        "mirror",
        "storm window",
        "skylight",
        "glass replacement",
        "commercial glazing",
        "residential glazing",
    ],
    "door": [
        "door",
        "doors",
        "garage door",
        "garage doors",
        "commercial door",
        "entrance",
        "entryway",
        "transom",
    ],
    "elevator": [
        "elevator",
        "elevators",
        "lift",
        "lifts",
        "escalator",
        "escalators",
        " dumbwaiter",
        "horizontal lift",
        "accessibility",
        "ada compliance",
    ],
    "siding": [
        "siding",
        "vinyl siding",
        "fiber cement",
        "stucco",
        "weather resistance",
        " exterior finish",
    ],
    "roofing_solar": [
        "solar",
        "solar panel",
        "solar installation",
        "photovoltaic",
        "pv system",
        "renewable energy",
        "green roof",
    ],
    "waterproofing": [
        "waterproofing",
        "damp proofing",
        "foundation waterproofing",
        "basement waterproofing",
        "tanking",
        "membrane",
    ],
    "insulation": [
        "insulation",
        "insulating",
        "thermal",
        "energy efficiency",
        "spray foam",
        "fiberglass",
        "cellulose",
        "rfi",
    ],
    "abatement": [
        "abatement",
        "asbestos",
        "lead",
        "mold",
        "mitigation",
        "environmental remediation",
        "hazardous material",
    ],
    "fire_protection": [
        "fire protection",
        "fire suppression",
        "sprinkler",
        "fire alarm",
        "fire safety",
        "fire stop",
    ],
}

# Reverse mapping: individual keyword -> set of industries it belongs to
_KEYWORD_TO_INDUSTRY: dict[str, set[str]] = {}


def _build_keyword_index() -> dict[str, set[str]]:
    """Build reverse index: keyword -> set of industries."""
    index: dict[str, set[str]] = {}
    for industry, keywords in INDUSTRY_EXPANSION.items():
        for kw in keywords:
            # Split multi-word phrases into individual words too
            index[kw.lower()] = index.get(kw.lower(), set())
            index[kw.lower()].add(industry)
            # Also index individual words from multi-word phrases
            for word in kw.split():
                if len(word) >= 3:
                    index[word.lower()] = index.get(word.lower(), set())
                    index[word.lower()].add(industry)
    return index


_KEYWORD_TO_INDUSTRY = _build_keyword_index()


def expand_industry(industry: str) -> set[str]:
    """Expand an industry search term into a set of searchable keywords.

    Takes a user-provided industry string (e.g. "Roofing") and returns
    a set of all relevant keywords including synonyms, trade terms,
    materials, and service types.

    Args:
        industry: Raw industry search term from the user query.

    Returns:
        Set of lowercase keywords for matching against company data.
    """
    if not industry:
        return set()

    industry_lower = industry.lower().strip()
    keywords: set[str] = set()

    # Direct match
    if industry_lower in INDUSTRY_EXPANSION:
        keywords.update(INDUSTRY_EXPANSION[industry_lower])
    else:
        # Try partial matches
        for key, value in INDUSTRY_EXPANSION.items():
            if key in industry_lower or industry_lower in key:
                keywords.update(value)

    # Also add individual words from the original query (3+ chars)
    words = set(re.findall(r"[a-z]{3,}", industry_lower))
    keywords.update(words)

    logger.debug("Expanded industry %r -> %d keywords", industry, len(keywords))
    return keywords


def matches_industry(company_text: str, industry: str) -> bool:
    """Check if company text matches an industry query with expansion.

    Uses the expanded keyword set to perform matching against company
    name, industry focus, and free-text fields.

    Args:
        company_text: Text to check (e.g. industry_focus field).
        industry: Original industry search term.

    Returns:
        True if any expanded keyword appears in the company text.
    """
    if not company_text or not industry:
        return False

    keywords = expand_industry(industry)
    text_lower = company_text.lower()

    for kw in keywords:
        if kw in text_lower:
            return True

    return False


def get_industry_for_keyword(keyword: str) -> set[str]:
    """Get all industries that a keyword belongs to.

    Useful for classification: given extracted text, determine which
    trade categories a company might belong to.

    Args:
        keyword: A single keyword or phrase.

    Returns:
        Set of industry names the keyword matches.
    """
    return _KEYWORD_TO_INDUSTRY.get(keyword.lower(), set())


def classify_company(
    company_name: str,
    industry_focus: str,
    description: str = "",
) -> list[str]:
    """Classify a company into one or more trade categories.

    Analyzes the company's name, stated industry focus, and any
    available description to determine the most likely trade
    categories it belongs to.

    Args:
        company_name: Legal company name.
        industry_focus: Stated industry focus area.
        description: Optional additional text context.

    Returns:
        List of matched industry category names (sorted by confidence).
    """
    combined = f"{company_name} {industry_focus} {description}".lower()
    scores: dict[str, int] = {}

    for industry, keywords in INDUSTRY_EXPANSION.items():
        score = 0
        for kw in keywords:
            if kw in combined:
                # Multi-word phrases score higher
                score += len(kw.split()) * 2 if " " in kw else 1
        if score > 0:
            scores[industry] = score

    # Return sorted by score descending
    return sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
