"""Heuristic email verifier — P5-Lite: confidence tiers without port 25.

RCPT probing is blocked from every network the platform controls (AWS
Trust & Safety rejection on IPv4, the same throttle on IPv6, consumer
ISPs), so this module answers "how deliverable does this address look?"
from evidence that IS reachable:

  1. DNS MX — authoritative: a domain with no MX (or a null MX, RFC 7505)
     can never receive mail, and a domain that does not exist cannot
     either. Resolver FAILURES are a separate, honest "unknown" — an
     offline DNS check must never mark a lead dead.
  2. Provider fingerprint — an MX at google.com / protection.outlook.com
     etc. means the domain's mail runs on major infrastructure.
  3. Disposable-domain blacklist — temp/throwaway domains are dead leads.
  4. Role-account detection — info@/contact@ is usually deliverable but
     generic; it ranks below a person-shaped address.
  5. Bounce learning — REAL outcomes from our own outreach
     (:mod:`app.email.bounce_learning`): a reply confirms the mailbox, a
     DSN kills it.

Honesty rules (CLAUDE.md §1/§12):
  * ``confidence`` is a HEURISTIC, never a verification claim. It never
    upgrades ``EmailVerificationTier`` — "high" means "high probability
    deliverable", not "verified". Only the P5 RCPT probe (when a network
    allows it) or a real reply ever proves a mailbox.
  * ``"dead"`` is only returned on AUTHORITATIVE evidence: bad syntax,
    a disposable domain, an authoritative no-MX, or a real bounce. An
    unreachable DNS resolver yields ``"unknown"`` and changes nothing.
  * Every verdict carries its reasons — no silent scores.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.email.email_validator import is_valid_email

logger = logging.getLogger(__name__)

#: Well-known disposable/temp-mail domains (curated; the blacklist grows
#: from real observations, never from guesses). Exact-match on the domain.
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "10minutemail.com",
    "tempmail.com", "temp-mail.org", "yopmail.com", "throwawaymail.com",
    "sharklasers.com", "getnada.com", "dispostable.com", "maildrop.cc",
    "mailnesia.com", "trashmail.com", "fakeinbox.com", "mytemp.email",
    "moakt.com", "tempail.com", "emailondeck.com", "burnermail.io",
    "spam4.me", "grr.la", "spamgourmet.com", "mailcatch.com",
    "inboxbear.com", "tempr.email", "discard.email", "mail-temp.com",
    "mohmal.com", "tmpmail.org", "tmpeml.com", "fake-mail.net",
})

#: Local parts that belong to a role, not a person. A role address is
#: deliverable most of the time but never a decision-maker's own inbox.
ROLE_LOCALS = frozenset({
    "info", "contact", "admin", "administrator", "sales", "support",
    "office", "hello", "mail", "webmaster", "no-reply", "noreply",
    "donotreply", "billing", "accounts", "accounting", "careers", "jobs",
    "team", "inquiries", "enquiry", "service", "help", "marketing",
    "reception", "frontdesk", "front-desk", "hr", "legal", "pr", "press",
    "media", "shop", "store", "orders", "newsletter", "notices",
    "postmaster", "abuse", "security", "privacy", "feedback",
})

#: MX-host suffix -> provider name. Matched against every MX host,
#: longest suffix first. A hit means the domain's mail runs on that
#: provider's infrastructure (the MX record itself is the proof).
PROVIDER_FINGERPRINTS: tuple[tuple[str, str], ...] = (
    ("google.com", "google"),
    ("googlemail.com", "google"),
    ("protection.outlook.com", "microsoft"),
    ("outlook.com", "microsoft"),
    ("hotmail.com", "microsoft"),
    ("yahoodns.net", "yahoo"),
    ("zoho.com", "zoho"),
    ("proton.me", "proton"),
    ("protonmail.ch", "proton"),
    ("messagingengine.com", "fastmail"),
    ("icloud.com", "icloud"),
    ("pphosted.com", "proofpoint"),
    ("mimecast.com", "mimecast"),
    ("barracudanetworks.com", "barracuda"),
    ("messagelabs.com", "symantec"),
    ("emailsrvr.com", "rackspace"),
    ("secureserver.net", "godaddy"),
    ("mailgun.org", "mailgun"),
)

#: Confidence tiers, strongest first. "dead" is authoritative-DOA;
#: "unknown" is an honest "could not judge" (never a negative).
CONFIDENCE_TIERS = ("high", "medium", "low", "unknown", "dead")


def _provider_of(mx_hosts: list[str]) -> str:
    """The provider whose infrastructure serves these MX hosts, or ''."""
    best = ""
    best_len = 0
    for host in mx_hosts:
        for suffix, name in PROVIDER_FINGERPRINTS:
            if host.endswith("." + suffix) or host == suffix:
                if len(suffix) > best_len:
                    best, best_len = name, len(suffix)
    return best


def mx_status(
    domain: str, *, timeout: float = 3.0,
    lookup: Callable[[str], list[str]] | None = None,
) -> tuple[str, list[str] | None]:
    """Authoritative MX state for one domain.

    Returns ``("hosts", [...])`` — the MX hosts, primary first;
    ``("none", [])`` — AUTHORITATIVELY no mail (NXDOMAIN, or no MX
    record, or a null MX RFC 7505): mail to this domain can never land;
    ``("unknown", None)`` — the resolvers could not answer (offline,
    timeouts): an honest unknown, never a verdict.

    ``lookup`` (tests) replaces the whole DNS path; it returns [] for
    "no MX" and raises for "resolver failure".
    """
    if lookup is not None:
        try:
            return lookup(domain)
        except Exception:  # noqa: BLE001 — injected failure = unknown
            return ("unknown", None)

    import dns.exception
    import dns.resolver

    from app.email.domain_verifier import _FAST_DNS_RESOLVERS

    saw_authoritative_none = False
    for ns in _FAST_DNS_RESOLVERS:
        resolver = dns.resolver.Resolver()
        resolver.nameservers = [ns]
        resolver.timeout = timeout
        resolver.lifetime = timeout + 1.0
        try:
            answer = resolver.resolve(domain, "MX")
            pairs = sorted(
                (int(r.preference), str(r.exchange).rstrip(".").lower())
                for r in answer
            )
            hosts = [h for _, h in pairs if h and h != "."]
            if hosts:
                return ("hosts", hosts)
            # A null MX (RFC 7505): the domain publishes "we accept no
            # mail" — authoritative.
            return ("none", [])
        except dns.resolver.NXDOMAIN:
            # The domain itself does not exist — authoritative.
            return ("none", [])
        except dns.resolver.NoAnswer:
            # The domain exists but publishes no MX — authoritative.
            saw_authoritative_none = True
            continue
        except Exception:  # noqa: BLE001 — one dead resolver -> next
            continue
    if saw_authoritative_none:
        return ("none", [])
    return ("unknown", None)


def classify_email(
    email: str,
    *,
    mx_lookup: Callable[[str], tuple[str, list[str] | None]] | None = None,
    outcome_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """One email's heuristic deliverability verdict.

    Returns ``{"confidence", "reasons", "role", "provider"}``.
    ``confidence`` is one of :data:`CONFIDENCE_TIERS`; ``reasons`` lists
    the evidence in decision order. See the module docstring for the
    honesty contract — this NEVER claims verification.
    """
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return {"confidence": "dead", "reasons": ["bad_syntax"],
                "role": False, "provider": ""}
    local, _, domain = addr.rpartition("@")
    if not is_valid_email(addr):
        return {"confidence": "dead", "reasons": ["bad_syntax"],
                "role": False, "provider": ""}
    if domain in DISPOSABLE_DOMAINS:
        return {"confidence": "dead", "reasons": ["disposable"],
                "role": False, "provider": ""}

    # Real outcome evidence outranks everything else.
    if outcome_lookup is not None:
        outcome = outcome_lookup(addr)
        if outcome == "bounced":
            return {"confidence": "dead", "reasons": ["bounced_before"],
                    "role": local in ROLE_LOCALS, "provider": ""}
        if outcome == "delivered":
            return {"confidence": "high", "reasons": ["reply_confirmed"],
                    "role": local in ROLE_LOCALS, "provider": ""}

    try:
        state, hosts = (mx_lookup(domain) if mx_lookup is not None
                        else mx_status(domain))
    except Exception:  # noqa: BLE001 — a failing check is an unknown, not a verdict
        state, hosts = "unknown", None
    if state == "unknown":
        # Could not check — an honest unknown, never a negative verdict.
        return {"confidence": "unknown", "reasons": ["mx_unresolvable"],
                "role": local in ROLE_LOCALS, "provider": ""}
    if state == "none":
        return {"confidence": "dead", "reasons": ["no_mx"],
                "role": False, "provider": ""}

    provider = _provider_of(hosts or [])
    role = local in ROLE_LOCALS
    if role:
        return {"confidence": "medium", "reasons": ["role_account"],
                "role": True, "provider": provider}
    if provider:
        return {"confidence": "high", "reasons": [f"provider_{provider}"],
                "role": False, "provider": provider}
    return {"confidence": "medium", "reasons": ["selfhosted_mx"],
            "role": False, "provider": ""}


def classify_emails(
    emails: list[str],
    *,
    mx_lookup: Callable[[str], tuple[str, list[str] | None]] | None = None,
    outcome_lookup: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """Classify many emails with ONE MX check per unique domain.

    Same verdicts as :func:`classify_email`, same order as the input.
    """
    domain_state: dict[str, tuple[str, list[str] | None]] = {}
    verdicts: list[dict[str, Any]] = []
    for email in emails:
        addr = (email or "").strip().lower()
        domain = addr.rpartition("@")[2] if "@" in addr else ""
        if domain and domain not in domain_state:
            # Cheap pre-checks that need no MX at all get it from the
            # shared path below; only cache the DNS call per domain. A
            # failing check (injected or real) caches as an honest
            # unknown — never a verdict.
            try:
                domain_state[domain] = (
                    mx_lookup(domain) if mx_lookup is not None
                    else mx_status(domain)
                )
            except Exception:  # noqa: BLE001 — unknown, not a verdict
                domain_state[domain] = ("unknown", None)
        # Re-classify with a domain-cached MX wrapper: every rule in
        # classify_email still applies (syntax/disposable/outcomes run
        # per email; only DNS is shared).
        cached = domain_state.get(domain)
        wrapped = (lambda d, s=cached: s) if cached is not None else None
        verdicts.append(classify_email(
            addr, mx_lookup=wrapped, outcome_lookup=outcome_lookup,
        ))
    return verdicts


_store = None


def get_email_classifier() -> Callable[[str], dict[str, Any]]:
    """The production single-email classifier, wired to the bounce store.

    Built lazily and cached; a broken bounce store degrades to
    heuristic-only (outcome_lookup=None) rather than failing the run —
    the same fail-open shape the pipeline's other learned gates use.
    """
    global _store
    if _store is None:
        try:
            from app.email.bounce_learning import BounceStore

            _store = BounceStore()
        except Exception:  # noqa: BLE001 — heuristics never hard-fail
            logger.info("bounce store unavailable — heuristic-only "
                        "classification", exc_info=True)
            _store = False  # type: ignore[assignment]
    lookup = _store.lookup if _store else None

    def classify(email: str) -> dict[str, Any]:
        return classify_email(email, outcome_lookup=lookup)

    return classify
