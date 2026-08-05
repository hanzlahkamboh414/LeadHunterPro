"""Company normalizer for consistent naming and comparison.

Provides robust normalization of company names for deduplication and
comparison purposes. Never modifies display names — only creates
normalized keys for internal comparison.
"""

from __future__ import annotations

import re

# Suffixes to strip during normalization (order matters: longer first)
_SUFFIX_PATTERNS = [
    # Full forms first
    r"\s+Incorporated\s*$",
    r"\s+Corporation\s*$",
    r"\s+Limited\s*$",
    r"\s+Holdings\s*$",
    # Abbreviations
    r"\s+Inc\.?\s*$",
    r"\s+Ltd\.?\s*$",
    r"\s+Corp\.?\s*$",
    r"\s+Co\.?\s*$",
    r"\s+LLC\s*$",
    r"\s+L\.L\.C\.?\s*$",
    # Generic descriptors
    r"\s+Construction\s*$",
    r"\s+Contractors?\s*$",
    r"\s+Builders?\s*$",
    r"\s+General\s+Contractors?\s*$",
]

# Reverse mapping for industry keyword groups
_INDUSTRY_GROUPS: dict[str, list[str]] = {
    "general_contractor": [
        "general contractor",
        "general contractors",
        "gc",
        "contracting",
        "construction company",
        "construction companies",
        "commercial contractor",
        "commercial contractors",
        "building contractor",
        "building contractors",
    ],
    "commercial_construction": [
        "commercial construction",
        "commercial builder",
        "commercial builders",
        "construction estimating",
        "estimating",
        "cost consulting",
        "project management",
    ],
    "heavy_civil": [
        "heavy civil",
        "civil engineering",
        "infrastructure",
        "highways",
        "roads",
        "bridges",
        "paving",
    ],
    "residential": [
        "residential construction",
        "residential builder",
        "home builder",
        "home builders",
        "remodeling",
        "renovation",
    ],
}


def normalize_name(name: str) -> str:
    """Normalize a company name for comparison.

    Strips suffixes, collapses whitespace, and lowercases.

    Args:
        name: Raw company name.

    Returns:
        Normalized name string for comparison.
    """
    if not name:
        return ""

    normalized = name.strip()

    # Apply suffix patterns in order
    for pattern in _SUFFIX_PATTERNS:
        normalized = re.sub(pattern, "", normalized, flags=re.IGNORECASE)

    # Collapse whitespace
    normalized = re.sub(r"\s+", " ", normalized).strip()

    return normalized.lower()


def extract_industry_keywords(industry: str) -> set[str]:
    """Extract keywords from industry string for matching.

    Args:
        industry: Industry search term.

    Returns:
        Set of lowercase keywords.
    """
    if not industry:
        return set()

    # Extract multi-word phrases first
    phrases: set[str] = set()
    for group_keywords in _INDUSTRY_GROUPS.values():
        for kw in group_keywords:
            if kw in industry.lower():
                phrases.add(kw)

    # Also extract individual words (3+ chars)
    words = set(re.findall(r"[a-z]{3,}", industry.lower()))

    return phrases | words


def calculate_similarity(name1: str, name2: str) -> float:
    """Calculate similarity between two company names (0.0 to 1.0).

    Uses word overlap and substring matching.

    Args:
        name1: First company name.
        name2: Second company name.

    Returns:
        Similarity score between 0.0 and 1.0.
    """
    if not name1 or not name2:
        return 0.0

    words1 = set(name1.lower().split())
    words2 = set(name2.lower().split())

    if not words1 or not words2:
        return 0.0

    # Jaccard similarity
    intersection = words1 & words2
    union = words1 | words2
    jaccard = len(intersection) / len(union) if union else 0.0

    # Substring check (one contained in other)
    sub_score = 0.0
    if name1.lower() in name2.lower() or name2.lower() in name1.lower():
        sub_score = 0.8

    return max(jaccard, sub_score)


def is_similar_name(name1: str, name2: str, threshold: float = 0.7) -> bool:
    """Check if two names are similar enough to consider duplicates.

    Args:
        name1: First company name.
        name2: Second company name.
        threshold: Minimum similarity score.

    Returns:
        True if names are considered similar.
    """
    return calculate_similarity(name1, name2) >= threshold
