"""Email discovery and cleaning utilities."""

import logging
import re

logger = logging.getLogger(__name__)

EMAIL_CLEAN_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def clean_emails(raw_emails: list[str]) -> list[str]:
    """Validate and deduplicate a raw list of email addresses.

    Args:
        raw_emails: Unfiltered email strings.

    Returns:
        Deduplicated, lowercased list of valid-looking emails.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for email in raw_emails:
        email = email.strip().lower()
        if email in seen:
            continue
        if EMAIL_CLEAN_PATTERN.fullmatch(email):
            seen.add(email)
            cleaned.append(email)
    return cleaned
