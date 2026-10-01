"""Conservative evidence gate for permanently invalid recipient addresses."""

from __future__ import annotations

import html
import re


_ADDRESS = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_INVALID = re.compile(
    r"\b5\.1\.[01]\b|\baddress not found\b|\buser unknown\b|"
    r"\bno such user\b|\brecipient (?:address )?(?:not found|does not exist)\b|"
    r"\b(?:email )?account (?:that you tried to reach )?(?:does not exist|is inactive|is disabled)\b|"
    r"\buser email address is marked as invalid\b|\bmailbox (?:does not exist|is disabled|is inactive)\b",
    re.I,
)
_TRANSIENT = re.compile(
    r"\b(?:temporar(?:y|ily)|delayed|try again later|mailbox full|quota exceeded)\b|\b4\.\d\.\d\b",
    re.I,
)


def invalid_recipient_reason(text: str) -> bool:
    """Only explicit non-existent/disabled mailbox evidence warrants deletion."""
    message = html.unescape(text or "")
    return bool(_INVALID.search(message)) and not bool(_TRANSIENT.search(message))


def hard_bounce_target(text: str, sent_addresses: set[str]) -> str | None:
    """Return one exact sent recipient named by a hard-failure notice.

    Ambiguous notices naming multiple campaign recipients are ignored; a
    generic 'Delivery failed' subject and policy/quota failures are ignored.
    """
    if not invalid_recipient_reason(text):
        return None
    mentioned = {m.group().lower() for m in _ADDRESS.finditer(html.unescape(text or ""))}
    matched = mentioned.intersection(a.lower() for a in sent_addresses)
    return next(iter(matched)) if len(matched) == 1 else None
