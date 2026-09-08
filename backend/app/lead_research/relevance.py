"""Deterministic evidence-relevance filter (CLAUDE.md §12 — noise never
passes as production research).

Root cause of the user's "irrelevant data keeps coming in": the LinkedIn
person-profile lane asked a small model for "skills / activity / posts /
hiring signals", and it free-associated personal-profile filler — connection
counts, follower counts, education history, certifications, honour-society
memberships, languages — none of which is a business signal an estimator
lead-gen platform can act on.

The filter is DETERMINISTIC (zero credits, zero AI): a claim in the LinkedIn
PERSON lane is dropped when its text matches a social-noise pattern.

Deliberately narrow and source-scoped. It is applied ONLY to the profile lane
(the caller decides where) and never to company facts, so legitimately
business-relevant claims — licenses, HUBZone certs, offices, capabilities —
are never at risk. "licen*" is explicitly NOT matched: a contractor licence is
a business signal, while an academic/professional certification is not.
"""

from __future__ import annotations

import logging
import re

from app.lead_research.models import AIEvidence

logger = logging.getLogger(__name__)

#: Claim patterns that are social-profile noise in the LinkedIn PERSON lane.
_NOISE_PATTERNS: list[re.Pattern[str]] = [
    # Connection / follower counts — "500+ connections", "5,412 followers"
    re.compile(r"\b\d{1,4}(?:[,\s]\d{3})*\+?\s*(?:connections|mutual connections|followers?)\b", re.I),
    # Follower / follow word alone (profile filler)
    re.compile(r"\b(?:followers?)\b", re.I),
    # Education history — schools, degrees, alumni
    re.compile(
        r"\b(attended|studied?\s+(?:at|in)|graduat(?:ed|ing)?\s+from|degree\s+in|university|alumn[ui]\w*|education)\b",
        re.I,
    ),
    # Academic honour societies / fraternities / sororities
    re.compile(r"\b(omicron\s+delta\s+kappa|delta\s+kappa|honor\s+society|fraternity|sorority)\b", re.I),
    # Certifications (CITI, certifications, "certified in …") — NOT licen*
    re.compile(r"\b(certifi\w*|citi)\b", re.I),
    # Languages — "speaks Spanish", "fluent in English", "Languages: Spanish"
    re.compile(
        r"\b(?:speaks?|fluent\s+in|language(?:s)?\s*[:-]?)\s*(spanish|english|french|german|mandarin|cantonese|portuguese|arabic|italian|tagalog|hindi|urdu|punjabi|vietnamese|korean|japanese|russian|dutch|polish)\b",
        re.I,
    ),
]


def is_social_noise(claim: str) -> bool:
    """True when ``claim`` reads as social-profile filler, not a business fact.

    An empty claim is always dropped — an empty string is never useful
    evidence, and a model returning blanks should not fill the dossier with
    empty rows.
    """
    text = (claim or "").strip()
    if not text:
        return True
    return any(p.search(text) for p in _NOISE_PATTERNS)


def filter_social_noise(facts: list[AIEvidence]) -> list[AIEvidence]:
    """Drop social-noise facts from a LinkedIn profile lane, preserving order.

    Every drop is logged (CLAUDE.md §6 — honest logging: a run that removed
    noise is distinguishable from one that had none to remove).
    """
    kept: list[AIEvidence] = []
    for fact in facts:
        if is_social_noise(fact.claim):
            logger.info(
                "Relevance filter dropped LinkedIn noise: %r", (fact.claim or "")[:100],
            )
            continue
        kept.append(fact)
    return kept