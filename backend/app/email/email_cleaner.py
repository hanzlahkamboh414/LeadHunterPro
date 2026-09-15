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


#: RFC 2606 reserved documentation domains. No real mailbox can live there,
#: so an address like ``you@example.com`` is page boilerplate — a site's own
#: "enter your email" placeholder or a docs snippet scraped as if it were a
#: contact. CLAUDE.md §12 bans placeholder domains outright; observed live in
#: the pending pool 2026-09-15 (jane@example.com, email@example.com, ...).
PLACEHOLDER_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "example.edu",
    }
)

#: Machine keys that merely LOOK like mailboxes. A pure-hex local of 16+
#: chars is a Sentry DSN / Wixpress crash key / Zhihu crash id embedded in
#: page source (observed: 2062d0a4929b45348643784b5cb39c36@sentry.wixpress.com).
_ARTIFACT_HEX_LOCAL_RE = re.compile(r"^[0-9a-f]{16,}$")

#: Mailing-list archive locals: a 10+ digit timestamp prefix is a Message-ID
#: fragment from a listserv page (observed: 20260828153349.8061-1-odion@…).
_ARTIFACT_TIMESTAMP_RE = re.compile(r"^[0-9]{10,}[.-]")


def is_crawl_artifact(email: str) -> bool:
    """True when *email* is a tracking/telemetry/placeholder artifact, not a
    human contact.

    The web-crawl regex harvests addresses out of raw page source, and page
    source is full of machine strings shaped like addresses: Sentry DSNs,
    crash-report keys, mailing-list archive ids, and the site's own
    ``you@example.com`` form placeholder. None of these is a lead. This test
    is deliberately narrow and syntactic — a false drop of a REAL address
    (e.g. karl@sentrycontracting.com, an actual company) is worse than an
    occasional artifact slipping through, so only unambiguous shapes match:

      * domain is an RFC 2606 placeholder (example.com/.org/.net/.edu);
      * the local part is a pure hex key of 16+ characters;
      * the local part starts with a 10+ digit timestamp.

    Args:
        email: A full email address.

    Returns:
        True when the address is a crawl artifact and must not be stocked.
    """
    text = (email or "").strip().lower()
    if "@" not in text:
        return False
    local, _, domain = text.rpartition("@")
    if domain in PLACEHOLDER_DOMAINS:
        return True
    if _ARTIFACT_HEX_LOCAL_RE.match(local):
        return True
    if _ARTIFACT_TIMESTAMP_RE.match(local):
        return True
    return False


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
    """Validate, artifact-filter and deduplicate a raw list of emails.

    Args:
        raw_emails: Unfiltered email strings — typically regex hits scraped
            straight off raw page source, telemetry keys included.

    Returns:
        Deduplicated, lowercased list of valid-looking emails with crawl
        artifacts (placeholder domains, hex keys, timestamp locals) removed.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for email in raw_emails:
        email = email.strip().lower()
        if email in seen:
            continue
        if is_crawl_artifact(email):
            continue
        if EMAIL_CLEAN_PATTERN.fullmatch(email):
            seen.add(email)
            cleaned.append(email)
    return cleaned
