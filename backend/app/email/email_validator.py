"""Email validation utilities.

A thin name over :func:`app.email.email_cleaner.is_valid_email_syntax` — the
one address grammar in the codebase. This module used to carry its own regex,
which is how it came to accept ``own.@jose.elcook`` (a trailing dot in the
local part) long after the crawler's copy had been tightened: two grammars,
two behaviours, one defect (CLAUDE.md §14).

The contract here is SHAPE ONLY, kept deliberately: ``john@example.com`` is
valid by this function and worthless as a lead. Callers that need the real
gate — placeholder domains, no-reply boxes, social platforms — must use
:func:`app.email.email_cleaner.is_acceptable_email` instead.
"""

import logging

from app.email.email_cleaner import DOMAIN_PATTERN, is_valid_email_syntax

logger = logging.getLogger(__name__)

#: The domain grammar, re-published under its historical name because
#: ``app.email.domain_verifier`` gates its MX lookups on it. Same compiled
#: object, not a copy — there is one domain rule in the codebase.
DOMAIN_RE = DOMAIN_PATTERN

__all__ = ["DOMAIN_RE", "is_valid_email"]


def is_valid_email(email: str) -> bool:
    """Return True if *email* is a syntactically well-formed address.

    Args:
        email: The email string to validate.

    Returns:
        True if the address parses. Says nothing about whether it is usable.
    """
    return is_valid_email_syntax(email)
