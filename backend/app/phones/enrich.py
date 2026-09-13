"""Phone-lead email enrichment — find the email behind a phone number.

A phone lead from a license board carries business name + person name +
city/state but never an email (verified 2026-09-13: no US board publishes
one). This module closes that gap with the company's OWN website:

    business name + city/state  ->  web search  ->  official site
    official site (+ contact pages)  ->  HTMLParser  ->  emails on it

Honesty rules (CLAUDE.md §1/§12):

* Only emails LITERALLY SEEN on the company's own pages are returned by the
  crawl stage — a page on Yelp/Facebook is a listing, not the company.
* When the crawl finds nothing, the P5 pattern-inference engine runs as a
  SECOND stage on the same site: the standard ``first.last@domain``
  permutations for the lead's person, verified against the company's own
  mail server (MX + RCPT, no message ever sent). Only an address the
  server CONFIRMS is returned — still an answer, never a guess.
* No findable website, no email on it, and no verified permutation  ->  an
  empty outcome, honestly ''. Never a placeholder, never a filler.

Everything is injected (search_fn / fetch_fn) so tests stay hermetic; the
production defaults reuse the platform's existing seams (§14): the search
provider registry, the discovery sources' never-raises fetch, the crawler
HTML parser, and the identity verifier's aggregator blocklist.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from urllib.parse import urlparse

from app.crawlers.html_parser import HTMLParser
from app.discovery.sources._http import fetch as _default_fetch
from app.email.pattern_inference import infer_verified_email

logger = logging.getLogger(__name__)

# Reuse the identity verifier's blocklist (§14) — a search result pointing
# at one of these hosts is a LISTING about the company, never the company's
# own website, so its emails belong to the directory, not the lead.
from app.engines.verification.identity_verifier import _AGGREGATOR_HOSTS

#: Emails that are page furniture, not contacts (tracking pixels disguised
#: as mailto:, template placeholders, the classic wix/yelp sentinels).
_JUNK_EMAIL_TOKENS = (
    "example.com", "example.org", "domain.com", "yourdomain", "email.com",
    "sentry.io", "sentry-next", "wixpress.com", "godaddy.com", "no-reply",
    "noreply@", "donotreply",
)
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")

#: How many extra same-site pages to try when the homepage shows no email —
#: the contact page is where contractors put it.
_MAX_CONTACT_PAGES = 2


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower().removeprefix("www.")


def _is_aggregator(url: str) -> bool:
    host = _host(url)
    if not host:
        return True
    return host in _AGGREGATOR_HOSTS or any(
        host.endswith("." + h) for h in _AGGREGATOR_HOSTS
    )


def _clean_emails(raw: list[str]) -> list[str]:
    """Drop page-furniture addresses; lowercase, dedup, keep order."""
    seen: list[str] = []
    for e in raw:
        e = (e or "").strip().lower().rstrip(".")
        if "@" not in e or "." not in e.rsplit("@", 1)[-1]:
            continue
        if any(t in e for t in _JUNK_EMAIL_TOKENS):
            continue
        if e.endswith(_IMAGE_EXTS):
            continue
        if e not in seen:
            seen.append(e)
    return seen


def _default_search(query: str, num: int) -> list[str]:
    """Search the web via the provider registry (SearXNG primary, free).

    Runs its own event loop: the enrichment worker is a plain thread, and
    the registry's providers are aiohttp-based coroutines. An empty
    registry or an all-providers-failed pass returns [] — logged honestly
    by the manager (CLAUDE.md §5), surfaced here as "no website found".
    """
    from app.search_providers.manager import SearchProviderManager
    from app.search_providers.models import SearchQuery

    async def _run() -> list[str]:
        response = await SearchProviderManager().search(
            SearchQuery(keywords=query, num_results=num),
        )
        return [r.url for r in response.results]

    return asyncio.run(_run())


def _pick_best_email(emails: list[str], website: str) -> str:
    """Prefer an address on the company's own domain; else first seen.

    Own-domain beats a free-mail box that happens to be listed too — but a
    gmail on the company's own contact page is REAL evidence for a calling
    user, so it is kept as the fallback (the emails-vertical feed applies
    its own stricter free-mail rule at its intake).
    """
    host = _host(website)
    for e in emails:
        e_host = e.rsplit("@", 1)[-1]
        if host and (e_host == host or e_host.endswith("." + host)):
            return e
    return emails[0] if emails else ""


def enrich_lead(
    lead: dict[str, Any],
    *,
    search_fn: Callable[[str, int], list[str]] | None = None,
    fetch_fn: Callable[..., Any] | None = None,
    infer_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Find one phone lead's email; return ``{email, email_source, website,
    dork}``.

    ``email`` is '' when nothing findable — an honest miss, never a guess.
    ``email_source`` says HOW it was found: ``website`` (literally seen on
    the company's own page) or ``pattern_inference`` (the mail server
    confirmed the permutation). ``dork`` tags the emails-vertical feed lane.
    ``website`` is recorded even without an email: it is the provenance of
    the attempt and the input P5's pattern-inference engine needs.
    """
    search = search_fn or _default_search
    do_fetch = fetch_fn or _default_fetch
    do_infer = infer_fn or infer_verified_email
    parser = HTMLParser()

    business = (lead.get("business_name") or "").strip()
    if not business:
        return {"email": "", "email_source": "", "website": "", "dork": ""}

    query = f'"{business}"'
    city = (lead.get("city") or "").strip().title()
    state = (lead.get("state") or "").strip().upper()
    if city:
        query += f" {city}"
    if state:
        query += f" {state}"

    urls = search(query, 10)
    site = next((u for u in urls if not _is_aggregator(u)), "")
    if not site:
        logger.info(
            "enrich %r: no official website among %d result(s) — honest miss",
            business, len(urls),
        )
        return {"email": "", "email_source": "", "website": "", "dork": ""}

    # Homepage first, then up to two contact-ish pages on the same host.
    emails: list[str] = []
    queue: list[str] = [site]
    checked = 0
    while queue and checked < 1 + _MAX_CONTACT_PAGES:
        url = queue.pop(0)
        checked += 1
        result = do_fetch(url, timeout=12.0)
        if not result.ok or not result.text:
            continue
        parsed = parser.parse(result.text, url)
        emails.extend(_clean_emails(parsed.emails))
        if url == site:
            # Contact pages are discovered from the homepage only — a contact
            # page linking another contact page adds nothing.
            queue.extend(
                u for u in parsed.links
                if _host(u) == _host(site)
                and "contact" in urlparse(u).path.lower()
            )
        if emails:
            break

    email = _pick_best_email(emails, site)
    if email:
        return {
            "email": email,
            "email_source": "website",
            "website": site,
            "dork": "phone_enrichment",
        }

    # Stage 2 (P5): the site is real but shows no email — ask the company's
    # own mail server about the standard permutations for this person. Only
    # a server-CONFIRMED address passes (MX + RCPT, catch-all aware, no
    # message ever sent); everything else is an honest miss with a reason.
    person = (lead.get("person_name") or "").strip()
    if person:
        inferred = do_infer(person, site)
        if inferred.get("email"):
            return {
                "email": inferred["email"],
                "email_source": "pattern_inference",
                "website": site,
                "dork": "pattern_inference",
            }
        logger.info(
            "enrich %r: pattern inference %s — honest miss",
            business, inferred.get("reason", "unknown"),
        )
    else:
        logger.info(
            "enrich %r: website %s shows no email and the lead carries no "
            "person name to infer from — honest miss", business, site,
        )
    return {"email": "", "email_source": "", "website": site, "dork": ""}
