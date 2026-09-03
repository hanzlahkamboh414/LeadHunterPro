"""Email discovery and cleaning utilities."""

import logging
import re

logger = logging.getLogger(__name__)

EMAIL_CLEAN_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Consumer webmail and ISP mail domains. These are NOT company domains, so a
#: contact reachable only at one of them gives us no website to enrich from.
#:
#: WHY THIS LIST EXISTS: the repo had no free-mail check anywhere (verified by
#: grep before adding this), and neither ``clean_emails`` nor ``is_valid_email``
#: covers it — both are purely syntactic. Without it, a plan-holder row like
#: "City Wide Construction Corp. / …@aol.com" hands ``aol.com`` downstream as
#: that company's domain, which is CLAUDE.md §12's "fake domain" failure in a
#: new costume. Both ``aol.com`` and ``gmail.com`` are observed in real
#: plan-holder PDFs, so this is a measured need, not a hypothetical one.
#:
#: Small trade contractors commonly use ISP mail, so the ISP domains matter as
#: much as the webmail ones. The list is deliberately conservative: a domain is
#: only listed when it is unambiguously consumer mail. Being absent from this
#: set is never treated as proof that a domain IS corporate.
FREE_MAIL_DOMAINS = frozenset(
    {
        # webmail
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "aol.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "gmx.com",
        "gmx.net",
        "zoho.com",
        "mail.com",
        "yandex.com",
        # US ISP mail (common for small trade contractors)
        "comcast.net",
        "sbcglobal.net",
        "att.net",
        "verizon.net",
        "bellsouth.net",
        "cox.net",
        "charter.net",
        "earthlink.net",
        "juno.com",
        "roadrunner.com",
        "rr.com",
        "windstream.net",
        "frontier.com",
        "embarqmail.com",
        "netzero.net",
        "mchsi.com",
    }
)


def is_free_mail_domain(email_or_domain: str) -> bool:
    """True when the address (or bare domain) is consumer webmail / ISP mail.

    Accepts either a full address (``bob@aol.com``) or a bare domain
    (``aol.com``) so callers do not each re-implement the split. Matching is
    exact on the registered domain — no suffix matching, because ``notaol.com``
    and ``aol.com.co`` are different domains and must not be swept up.

    Args:
        email_or_domain: A full email address or a bare domain string.

    Returns:
        True if the domain is a known consumer mail provider. False for an
        empty/garbled input — absence of proof is never proof of absence
        (CLAUDE.md accuracy rule: unknown is not true).
    """
    text = (email_or_domain or "").strip().lower().rstrip(".")
    if not text:
        return False
    domain = text.rsplit("@", 1)[-1] if "@" in text else text
    return domain in FREE_MAIL_DOMAINS


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
