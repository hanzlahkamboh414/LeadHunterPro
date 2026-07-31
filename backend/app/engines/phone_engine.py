"""Phone number extraction engine."""

import logging
import re

logger = logging.getLogger(__name__)

PHONE_PATTERN = re.compile(
    r"(?:\+\d{1,3}[\s\-]?)?"
    r"(?:\(?\d{2,4}\)?[\s\-]?)?"
    r"\d{3}[\s\-]?\d{3}[\s\-]?\d{4}"
)


def extract_phones(text: str) -> list[str]:
    """Extract phone numbers from raw text.

    Args:
        text: Source text to scan.

    Returns:
        List of cleaned phone number strings.
    """
    phones: list[str] = []
    for match in PHONE_PATTERN.findall(text):
        digits = re.sub(r"\D", "", match)
        if len(digits) >= 10:
            phones.append(match.strip())
    return sorted(set(phones))
