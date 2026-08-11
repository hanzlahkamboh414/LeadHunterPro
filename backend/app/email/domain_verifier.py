"""Email domain/MX verification — the V1 ``domain`` tier.

Increment 3: implements the ``domain`` tier of :class:`EmailVerificationTier`
from the Lead schema. A discovered address that passes syntax (``format``) is
upgraded to ``domain`` when its domain has at least one resolvable MX record —
the address belongs to a real mail host, not a fabricated domain.

Zero-budget constraint: the MX check uses a free, keyless DNS-over-HTTPS
resolver (``dns.google/resolve``) via ``requests`` — no API key, no extra
dependency. Failures (non-200, malformed JSON, network errors) are treated as
UNRESOLVED (False), never as verified — honest "unknown", per the V1 accuracy
rules. ``person_bound`` addresses are left untouched: their domain is a
separate, deferred concern in V1 (see the qualifying sample's concern,
``lead_samples.py``).

Offline tests monkeypatch ``requests`` so no live resolver is ever hit.
"""

from __future__ import annotations

import logging

import requests

from app.email.email_validator import DOMAIN_RE
from app.engines.lead.lead_models import EmailVerificationTier, LeadEmail

logger = logging.getLogger(__name__)

#: Free, keyless DNS-over-HTTPS JSON resolver used for MX lookups.
MX_LOOKUP_URL = "https://dns.google/resolve"

#: DNS record type for MX answers (RFC 1035).
_MX_RECORD_TYPE = 15

#: Resolver failures that count as "unresolved". Captured at import so a
#: monkeypatched ``requests`` (offline tests) still resolves the tuple.
_NETWORK_ERRORS = (requests.RequestException, ValueError)


def domain_has_mx(domain: str, *, timeout: int = 10) -> bool:
    """True when *domain* has at least one MX record.

    Domains that fail ``DOMAIN_RE`` (e.g. "localhost") are rejected without a
    network call. Non-200 responses, malformed JSON, and network errors are
    treated as UNRESOLVED (False) — never a guess.
    """
    if not DOMAIN_RE.match(domain):
        return False
    try:
        response = requests.get(
            MX_LOOKUP_URL,
            params={"name": domain, "type": "MX"},
            headers={"Accept": "application/dns-json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            logger.warning("MX lookup non-200 for %s: %s", domain, response.status_code)
            return False
        data = response.json()
    except _NETWORK_ERRORS as exc:
        logger.warning("MX lookup failed for %s: %s", domain, exc)
        return False
    if not isinstance(data, dict):
        return False
    return any(
        entry.get("type") == _MX_RECORD_TYPE
        for entry in data.get("Answer") or []
    )


def verify_email_domains(emails: list[LeadEmail]) -> list[LeadEmail]:
    """Return *emails* with ``format``-tier addresses upgraded to ``domain``
    when their domain's MX record resolves.

    One MX lookup per unique domain (shared by every address on it);
    ``person_bound`` addresses are never re-verified or downgraded. Returns a
    new list; the input records are not mutated.
    """
    domains = {
        email.email.split("@", 1)[1]
        for email in emails
        if email.tier is EmailVerificationTier.format
        and "@" in email.email
        and DOMAIN_RE.match(email.email.split("@", 1)[1])
    }
    resolved = {domain: domain_has_mx(domain) for domain in domains}

    verified: list[LeadEmail] = []
    for email in emails:
        if email.tier is not EmailVerificationTier.format or "@" not in email.email:
            verified.append(email)
            continue
        domain = email.email.split("@", 1)[1]
        if DOMAIN_RE.match(domain) and resolved.get(domain):
            verified.append(
                LeadEmail(
                    email=email.email,
                    tier=EmailVerificationTier.domain,
                    source_url=email.source_url,
                    fetched_at=email.fetched_at,
                )
            )
        else:
            verified.append(email)
    return verified
