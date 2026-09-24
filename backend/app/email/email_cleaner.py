"""Email discovery and cleaning utilities.

THE single gate for "may we keep this string as an email?". Every path that
puts an address into lead data routes through :func:`is_acceptable_email` or
:func:`clean_emails` here — the crawl (``crawlers/html_parser``), the
plan-holder/people parsers, the website engine, the company extractor, the
pending pool and the phones enricher.

WHY THIS MODULE AND NOT FOUR. Before 2026-09-21 the rules were triplicated:
this module, a private ``_JUNK_EMAIL_TOKENS``/``_clean_emails`` pair inside
``phones/enrich.py`` (imported cross-module by ``phones/overture.py`` — a
private name reaching across packages), and a purely syntactic
``email_validator.is_valid_email``. The lists had drifted, and the drift was
the defect: ``email.com`` was known junk in the phones path and UNKNOWN to
this one, so ``test@email.com`` was filtered off a phone record and stored
as a research contact. One list, one place (CLAUDE.md §14).
"""

import logging
import re

logger = logging.getLogger(__name__)

#: The domain grammar, as a string fragment. It has two consumers with
#: different anchoring needs — a whole-domain test and the domain half of an
#: address — so it is written once here and anchored where each is built.
_DOMAIN_FRAGMENT = r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"

#: A whole domain, ANCHORED at both ends. Published because
#: ``domain_verifier`` gates its MX lookups on it, and ``re.match`` alone is
#: a prefix test: unanchored, ``acme.com<script>`` passes and we spend a DNS
#: call on a string that is not a domain. The hand-written pattern this
#: replaced carried its own ``$`` and refused it.
DOMAIN_PATTERN = re.compile(rf"^{_DOMAIN_FRAGMENT}$")

#: Local part: dot-atom — a dot may only SEPARATE atoms, never start, end or
#: repeat. The old grammar (``[A-Za-z0-9._%+-]+``) allowed all three, so
#: ``own.@jose.elcook`` was scraped, stored, and then spent 171.2s of
#: research on a run that produced nothing (audited live 2026-09-21).
_LOCAL_PATTERN = re.compile(r"[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*")

#: A well-formed address. Composed from the two fragments rather than written
#: out again, so the local and domain rules cannot drift apart from the
#: domain check the verifier uses.
EMAIL_CLEAN_PATTERN = re.compile(
    rf"{_LOCAL_PATTERN.pattern}@{_DOMAIN_FRAGMENT}"
)

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


#: Domains no contact of ours can live at: RFC 2606 reserved documentation
#: domains, template placeholders, and the platform/vendor domains that show
#: up as page furniture (a Wix or GoDaddy footer address belongs to the
#: PLATFORM, not to the company we are researching). CLAUDE.md §12 bans
#: placeholder domains outright; observed live in the pending pool
#: 2026-09-15 (jane@example.com, email@example.com, ...).
#:
#: The template group (``email.com``, ``domain.com``, ``yourdomain.*``) was
#: merged in from ``phones/enrich.py`` on 2026-09-21. It had lived ONLY in
#: that one path, which is exactly how ``test@email.com`` came to be
#: filtered off a phone record while being stored as a research contact.
#: Matching is exact on the registered domain — never a substring — so
#: ``karl@sentrycontracting.com`` (a real company) is untouched.
PLACEHOLDER_DOMAINS = frozenset(
    {
        # RFC 2606 reserved — no real mailbox can exist there.
        "example.com",
        "example.org",
        "example.net",
        "example.edu",
        # Template / "put your own domain here" strings.
        "email.com",
        "domain.com",
        "yourdomain.com",
        "yourdomain.net",
        "yourdomain.org",
        # Platform and vendor furniture.
        "sentry.io",
        "wixpress.com",
        "godaddy.com",
    }
)

#: Social platforms and job boards. An address on one of these is the
#: PLATFORM's or a stranger's, never the contracting company's contact — the
#: page it was scraped from was not the company's page at all. Observed live
#: 2026-09-21: a tiktok.com address pulled off a scraped page cost 171.2s of
#: research on a lead that was never real.
#:
#: ``x.com`` is deliberately ABSENT although it is X/Twitter's current domain
#: — ``twitter.com`` covers the platform. A single-character second-level
#: domain cannot be told apart from placeholder data, and this repo's tests
#: use ``x.com``/``y.com``/``z.com`` as their stand-in domains: banning it
#: would rewrite 23 fixture assertions whose subject is not social platforms
#: at all. Same doctrine as ``FREE_MAIL_DOMAINS`` — measured need, not
#: hypothetical — and the same reason ``is_crawl_artifact`` refuses to widen:
#: a false drop of a real address costs more than a junk one slipping past.
SOCIAL_PLATFORM_DOMAINS = frozenset(
    {
        "tiktok.com",
        "facebook.com",
        "fb.com",
        "instagram.com",
        "youtube.com",
        "twitter.com",
        "pinterest.com",
        "linkedin.com",
        "snapchat.com",
        "reddit.com",
        "threads.net",
        "whatsapp.com",
        "telegram.org",
        "tumblr.com",
    }
)

#: Local parts that belong to a robot, not a person. ANCHORED on purpose:
#: the phones path used to test these as substrings, which would also drop a
#: real person whose local merely contains the word (``noreplya@acme.com``)
#: and a mailbox like ``john.noreply@acme.com``. Only a local that IS one of
#: these forms — optionally with a ``+tag``/``.tag`` suffix — is dropped.
_NOREPLY_LOCAL_RE = re.compile(
    r"^(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply)(?:[+._-].*)?$"
)

#: Image and asset extensions. A crawl regex happily reads a filename like
#: ``logo@2x.png`` as an address — it is not one.
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")

#: A phone number glued to the FRONT of a local part.
#:
#: When a page puts a phone and an email in adjacent inline tags with no
#: whitespace between them, naive text extraction welds them together:
#: ``<span>620-6727</span><a>email.office@premierelectricalcontracting.com</a>``
#: yields ``620-6727email.office@premierelectricalcontracting.com``. The
#: result is perfectly well-formed — dot-atom local, dotted domain — so
#: syntax alone cannot refuse it, and it is not a placeholder, a robot or a
#: social platform either. Reported live 2026-09-21 on a real phone record,
#: where it had also WON the pick: ``sorted()`` orders ``6`` before ``e``,
#: so the welded string preceded the true address.
#:
#: Matched only when the local OPENS with a phone-SHAPED run (2-4 digits, a
#: separator, 2-4 more digits), so a real local is untouched:
#: ``24hrservice@``, ``365plumbing@``, ``24-7service@`` and
#: ``1800-flowers@`` all survive.
_PHONE_PREFIX_LOCAL_RE = re.compile(r"^\+?\d{2,4}[-.\s]\d{2,4}")

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

      * domain is in :data:`PLACEHOLDER_DOMAINS` — reserved, template, or
        platform-furniture domains, matched EXACTLY (never a substring);
      * the local part is a pure hex key of 16+ characters;
      * the local part starts with a 10+ digit timestamp.

    This is the ARTIFACT half of the gate only. Whether an address is one we
    would actually contact (no-reply boxes, social platforms, bad syntax) is
    :func:`is_acceptable_email`, which composes this with the other rules.

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


def is_valid_email_syntax(email: str) -> bool:
    """True when *email* is well-formed — SHAPE only, nothing else.

    Deliberately says nothing about whether the address is usable:
    ``john@example.com`` is well-formed and worthless. Kept separate so the
    format validators (``email_validator.is_valid_email``) keep meaning
    "does this parse?" and never silently acquire the junk-domain rules.
    """
    return bool(EMAIL_CLEAN_PATTERN.fullmatch((email or "").strip().lower()))


def is_noreply_address(email: str) -> bool:
    """True when the local part is a robot's (no-reply / donotreply).

    Anchored — see :data:`_NOREPLY_LOCAL_RE`. A human whose local merely
    contains the word is not swept up.
    """
    text = (email or "").strip().lower()
    if "@" not in text:
        return False
    return bool(_NOREPLY_LOCAL_RE.match(text.rpartition("@")[0]))


def is_social_platform_email(email: str) -> bool:
    """True when the address is on a social platform or job board.

    Not an artifact — a real mailbox — but never OUR company's contact, so
    it must not be stocked as one (see :data:`SOCIAL_PLATFORM_DOMAINS`).
    """
    text = (email or "").strip().lower().rstrip(".")
    if "@" not in text:
        return False
    return text.rpartition("@")[2] in SOCIAL_PLATFORM_DOMAINS


def is_acceptable_email(email: str) -> bool:
    """THE gate: may this string be kept as an email contact?

    Composes every rule, in the order that makes a failure diagnosable:

    1. **syntax** — dot-atom local, dotted domain (:func:`is_valid_email_syntax`)
    2. **asset**  — a filename the regex mis-read as an address
    3. **artifact** — placeholder/platform domain, hex key, listserv id
    4. **welded** — a phone number concatenated onto the local part
    5. **robot** — no-reply / donotreply local
    6. **platform** — a social platform's or job board's own domain

    Callers that only need one of these should call that predicate directly;
    every caller that stocks an address for a lead should call THIS one, so
    a rule added here reaches the crawl, the parsers, the pending pool and
    the phones enricher at once instead of being re-implemented per path.
    """
    text = (email or "").strip().lower()
    if not is_valid_email_syntax(text):
        return False
    if text.endswith(_ASSET_SUFFIXES):
        return False
    if is_crawl_artifact(text):
        return False
    if _PHONE_PREFIX_LOCAL_RE.match(text.rpartition("@")[0]):
        return False
    if is_noreply_address(text):
        return False
    return not is_social_platform_email(text)


def clean_emails(raw_emails: list[str]) -> list[str]:
    """Validate, filter and deduplicate a raw list of emails.

    Args:
        raw_emails: Unfiltered email strings — typically regex hits scraped
            straight off raw page source, telemetry keys included. ``None``
            entries are tolerated: callers feed this straight from parsed
            dicts where the key may be missing.

    Returns:
        Deduplicated, lowercased list in first-seen order, with every
        address that fails :func:`is_acceptable_email` removed.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for email in raw_emails:
        text = (email or "").strip().lower()
        if not text or text in seen:
            continue
        if not is_acceptable_email(text):
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned
