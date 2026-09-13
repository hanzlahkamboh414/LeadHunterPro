"""Email pattern-inference engine (P5) — permutations + MX + catch-all.

Consumes what P3.5 recorded: a phone lead with a ``website`` (the company's
official site, found by the enricher) but no email LITERALLY SEEN on it. The
engine generates the standard ``first.last@domain`` permutations for the
lead's person and asks the company's OWN mail server whether the mailbox
exists — an answer, not a guess.

ToS-safe by construction (the "no DATA" rule): the SMTP probe is a
HELO -> MAIL FROM -> RCPT TO -> RSET/QUIT handshake. No message is ever
sent — the conversation stops before the DATA command, the same shape the
email-verification industry uses. Nobody is emailed, so no spam surface.

Honesty rules (CLAUDE.md §1/§12):

* A permutation is returned ONLY when the mail server answers 250 for it
  AND the server is not a catch-all (a catch-all accepts anything, so its
  250 proves nothing — the outcome is an honest "unverifiable", never a
  maybe).
* Temporary failures (4xx greylisting, connect timeouts, no MX) are honest
  misses with a reason — never retried into a guess.
* Everything is injected (``mx_lookup`` / ``probe``) so tests stay
  hermetic; the production paths reuse the platform's existing seams
  (§14): dnspython against the domain verifier's fast resolvers.
"""

from __future__ import annotations

import logging
import random
import smtplib
from typing import Any, Callable
from urllib.parse import urlparse

from app.email.domain_verifier import _FAST_DNS_RESOLVERS

logger = logging.getLogger(__name__)

#: HELO name for the probe — an honest, non-impersonating internal label.
_HELO_DOMAIN = "verify.leadhunter.internal"

#: Connect timeout per SMTP host. MX hosts that cannot be reached inside
#: this window are skipped, not hung on.
_SMTP_TIMEOUT_S = 5.0

#: How many permutations are probed per domain (rate-friendly: the probe
#: stops at the FIRST verified address anyway, so this is the worst case).
MAX_CANDIDATES = 8

#: Name suffixes that are not part of anybody's email local-part.
_NAME_SUFFIXES = frozenset({
    "jr", "sr", "ii", "iii", "iv", "v", "jr.", "sr.", "lic", "lic.",
})


def _clean_token(token: str) -> str:
    """One name token as an email local-part fragment: lowercase, letters
    and digits only (O'Brien -> obrien, Jean-Luc -> jeanluc, St. -> st)."""
    return "".join(c for c in token.lower() if c.isalnum())


def _name_parts(raw: str) -> tuple[str, str]:
    """``(first, last)`` from a license-board person name.

    Handles the board's ``"LAST, FIRST"`` flip, middle names, and suffixes
    ("Robert Jones Jr."). Anything unparsable is an honest ``("", "")`` —
    a guessed name would silently mis-permutate.
    """
    s = (raw or "").strip()
    if not s:
        return "", ""
    if "," in s:
        last_raw, _, first_raw = s.partition(",")
        last_tokens = [
            t for t in last_raw.split() if t.lower() not in _NAME_SUFFIXES
        ]
        first_tokens = [
            t for t in first_raw.split() if t.lower() not in _NAME_SUFFIXES
        ]
        first = _clean_token(first_tokens[0]) if first_tokens else ""
        last = _clean_token(last_tokens[-1]) if last_tokens else ""
        return first, last
    tokens = [t for t in s.split() if t.lower() not in _NAME_SUFFIXES]
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return "", _clean_token(tokens[0])
    return _clean_token(tokens[0]), _clean_token(tokens[-1])


def email_permutations(
    person_name: str, domain: str, *, limit: int = MAX_CANDIDATES,
) -> list[str]:
    """The standard local-part patterns for one person, most common first.

    ``domain`` may be a bare domain OR the full website URL (the phone
    store's ``website`` column) — it is normalized here. Ordered by US
    small-business convention (``first.last`` dominates); a single-token
    name yields only the token itself; deduped, lowercase.
    """
    first, last = _name_parts(person_name)
    domain = domain_of(domain)
    if not domain or (not first and not last):
        return []

    candidates: list[str] = []
    f, l = first, last
    if f and l:
        candidates += [
            f"{f}.{l}", f"{f}{l}", f"{f}_{l}", f,
            f"{f[0]}{l}", f"{f[0]}.{l}", f"{f}{l[0]}",
            f"{l}.{f}", f"{l}{f}", l,
        ]
    elif f:
        candidates = [f]
    else:
        candidates = [l]

    seen: list[str] = []
    for local in candidates:
        email = f"{local}@{domain}"
        if email not in seen:
            seen.append(email)
    return seen[:limit]


def domain_of(website_or_domain: str) -> str:
    """The registrable host from a URL OR a bare domain — the ``website``
    column stores full URLs, MX lookups want the host."""
    s = (website_or_domain or "").strip()
    if not s:
        return ""
    if "://" in s:
        s = urlparse(s).netloc or ""
    host = s.split("/")[0].split(":")[0].lower().removeprefix("www.")
    return host


def mx_hosts(domain: str, *, timeout: float = 3.0) -> list[str]:
    """The domain's MX hosts, lowest preference (primary) first.

    Same fast-resolver path as :func:`domain_verifier.domain_has_mx_fast`
    (dnspython against public IPv4 resolvers — no key, no cost). A null MX
    (RFC 7505 ".") is a domain that accepts no mail and yields []. Every
    failure mode is an honest empty list.
    """
    import dns.resolver  # local import: keeps module import keyless-cheap

    for ns in _FAST_DNS_RESOLVERS:
        try:
            resolver = dns.resolver.Resolver()
            resolver.nameservers = [ns]
            resolver.timeout = timeout
            resolver.lifetime = timeout + 1.0
            answer = resolver.resolve(domain, "MX")
            pairs = sorted(
                (
                    int(r.preference), str(r.exchange).rstrip(".").lower()
                )
                for r in answer
            )
            return [host for _, host in pairs if host and host != "."]
        except Exception:  # noqa: BLE001 — a resolver failure is unresolved
            continue
    return []


def smtp_rcpt_accepts(
    host: str,
    email: str,
    *,
    timeout: float = _SMTP_TIMEOUT_S,
) -> bool | None:
    """Ask one MX host whether a mailbox exists.

    ``True`` = 250 (mailbox accepted), ``False`` = 5xx (rejected — the
    mailbox does not exist), ``None`` = inconclusive (4xx greylist, bad
    handshake, unreachable) — an honest unknown, never a guess.

    ToS-safe: the conversation ends at RCPT TO. DATA is never sent, so no
    message ever leaves the platform. The null sender (``MAIL FROM:<>``,
    RFC 5321) is used first — it is the bounce address, impersonating
    nobody; servers that refuse it get the RFC-mandated ``postmaster@``
    of their own domain as the fallback.
    """
    try:
        with smtplib.SMTP(host, 25, timeout=timeout) as smtp:
            code, _ = smtp.helo(_HELO_DOMAIN)
            if code != 250:
                return None
            for sender in ("", f"postmaster@{email.rsplit('@', 1)[-1]}"):
                code, _ = smtp.docmd(f"MAIL FROM:<{sender}>")
                if code == 250:
                    break
            else:
                return None
            code, _ = smtp.docmd(f"RCPT TO:<{email}>")
            if code == 250:
                return True
            if 500 <= code < 600:
                return False
            return None
    except (OSError, smtplib.SMTPException):
        return None


def _is_catch_all(host: str, domain: str, probe: Callable) -> bool | None:
    """True when the host accepts mail for a mailbox that cannot exist.

    A random local-part nobody would ever hold: if the server says 250 to
    it, every 250 it gives is meaningless. ``None`` = the probe itself was
    inconclusive (treated as unverifiable, never as a green light).
    """
    rand = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=10))
    return probe(host, f"zz{rand}-noexist@{domain}")


def infer_verified_email(
    person_name: str,
    website_or_domain: str,
    *,
    mx_lookup: Callable[[str], list[str]] | None = None,
    probe: Callable[[str, str], bool | None] | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> dict[str, Any]:
    """The engine's entry point: ``{"email": …, "reason": …}``.

    Reasons: ``verified`` (an email the mail server confirmed), ``no_name``,
    ``no_domain``, ``no_mx``, ``catch_all`` (unverifiable by design — never
    a maybe), ``unreachable``, ``greylisted``, ``all_rejected``.
    """
    lookup_mx = mx_lookup or mx_hosts
    do_probe = probe or smtp_rcpt_accepts

    domain = domain_of(website_or_domain)
    if not domain:
        return {"email": "", "reason": "no_domain"}
    candidates = email_permutations(person_name, domain,
                                     limit=max_candidates)
    if not candidates:
        return {"email": "", "reason": "no_name"}

    hosts = lookup_mx(domain)
    if not hosts:
        return {"email": "", "reason": "no_mx"}

    host = hosts[0]
    catch_all = _is_catch_all(host, domain, do_probe)
    if catch_all is None:
        return {"email": "", "reason": "unreachable"}
    if catch_all:
        return {"email": "", "reason": "catch_all"}

    outcomes: list[bool | None] = []
    for candidate in candidates:
        outcome = do_probe(host, candidate)
        outcomes.append(outcome)
        if outcome is True:
            logger.info(
                "pattern inference: %s verified for %r (host %s, %d probe(s))",
                candidate, person_name, host, len(outcomes) + 1,
            )
            return {"email": candidate, "reason": "verified"}
    if all(o is None for o in outcomes):
        return {"email": "", "reason": "greylisted"}
    return {"email": "", "reason": "all_rejected"}
