"""Email validation utilities."""

import logging
import re

logger = logging.getLogger(__name__)

DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email(email: str) -> bool:
    """Return True if *email* looks like a plausible email address.

    Args:
        email: The email string to validate.

    Returns:
        True if the address passes basic format checks.
    """
    parts = email.split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    return bool(local) and bool(DOMAIN_RE.match(domain))
