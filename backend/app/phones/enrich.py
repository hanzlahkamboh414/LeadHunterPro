"""Phone-lead email enrichment — find the email behind a phone number.

A phone lead from a license board carries business name + person name +
city/state but never an email (verified 2026-09-13: no US board publishes
one). This module closes that gap with the company's OWN website:

    business name + city/state  ->  web search  ->  official site
    official site (+ contact pages)  ->  HTMLParser  ->  emails on it

Honesty rules (CLAUDE.md §1/§12):

* Stage 0 (P8): the Overture places dataset carries emails for many
  phone numbers; one local phone-keyed lookup answers those for FREE
  before any search credit is spent. It is materialized data, not a
  guess — but it is still a dataset the business itself published, so a
  hit short-circuits the pipeline as a first-class source.
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
import re
from typing import Any, Callable
from urllib.parse import urlparse

from app.crawlers.html_parser import HTMLParser
from app.discovery.sources._http import fetch as _default_fetch
from app.email.email_cleaner import clean_emails
from app.email.pattern_inference import infer_verified_email
from app.discovery.tradefold import normalize_trade
from app.phones.store import normalize_phone

logger = logging.getLogger(__name__)

# Reuse the identity verifier's blocklist (§14) — a search result pointing
# at one of these hosts is a LISTING about the company, never the company's
# own website, so its emails belong to the directory, not the lead.
from app.engines.verification.identity_verifier import _AGGREGATOR_HOSTS

#: Page-furniture filtering lives in ONE place now. The local
#: ``_JUNK_EMAIL_TOKENS``/``_IMAGE_EXTS``/``_clean_emails`` trio that used to
#: sit here was a second, drifted copy of the rules: it knew ``email.com``
#: and the shared cleaner did not, which is precisely how ``test@email.com``
#: was filtered off a phone record yet stored as a research contact. Deleted
#: 2026-09-21 — ``clean_emails`` from ``app.email.email_cleaner`` is the gate
#: for scraped, dataset (Overture) and regex-hit addresses alike (CLAUDE.md §14).

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


def _default_search(query: str, num: int) -> list[str]:
    """Search the web via the provider registry (SearXNG primary, free).

    Runs its own event loop: the enrichment worker is a plain thread, and
    the registry's providers are aiohttp-based coroutines. An empty
    registry or an all-providers-failed pass returns [] — logged honestly
    by the manager (CLAUDE.md §5), surfaced here as "no website found".
    """
    from app.search_providers.manager import SearchProviderManager
    from app.search_providers.models import SearchQuery
    from app.search_providers.registry import get_registry

    registry = get_registry()

    async def _run() -> list[str]:
        try:
            response = await SearchProviderManager(registry).search(
                SearchQuery(keywords=query, num_results=num),
            )
            return [r.url for r in response.results]
        finally:
            # asyncio.run closes this loop after _run returns. Provider
            # sessions belong to that loop and must be closed before then.
            for provider in registry.get_enabled():
                close = getattr(provider, "close", None)
                if close:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 — cleanup cannot hide search results
                        logger.warning("phone search provider close failed", exc_info=True)

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


def _default_overture(phone: str) -> dict[str, str]:
    """The production stage-0 lookup: Overture's phone-keyed email table.

    Returns ``{}`` whenever the local ``overture.duckdb`` has not been
    synced yet (or anything goes wrong) — stage 0 is an accelerator over
    the local materialization of a FREE dataset, never a dependency.
    """
    try:
        from app.phones.overture import OvertureStore

        hits = OvertureStore().lookup_emails([phone])
        return hits.get(phone, {})
    except Exception:  # noqa: BLE001 — stage 0 must never break enrichment
        logger.info("overture stage 0 unavailable — honest empty", exc_info=True)
        return {}


def resolve_trade(
    lead: dict[str, Any], *,
    search_fn: Callable[[str, int], list[str]] | None = None,
    fetch_fn: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Verify a raw phone lead's trade on its own website, or leave it raw.

    A board business name is only a search key. The fetched page must show
    both the same business and the same phone, then independently describe
    one canonical trade outside the business name. The exact fetched page is
    retained as evidence. No person is inferred from a business name.
    """
    empty = {"trade": "", "evidence_url": "", "evidence_kind": ""}
    business = (lead.get("business_name") or "").strip()
    phone = normalize_phone(lead.get("phone", ""))
    words = re.findall(r"[a-z0-9]+", business.lower())
    while words and words[-1] in {"llc", "inc", "corp", "corporation", "ltd", "co"}:
        words.pop()
    if len(words) < 2 or not phone:
        return empty
    identity = " ".join(words)
    query = f'"{business}" {lead.get("city", "")} {lead.get("state", "")}'
    search = search_fn or _default_search
    fetch = fetch_fn or _default_fetch
    parser = HTMLParser()
    from app.search_providers.contractor_classifier import ContractorClassifier

    classifier = ContractorClassifier()
    candidates = [url for url in search(query.strip(), 6) if not _is_aggregator(url)]
    for url in candidates[:3]:
        result = fetch(url, timeout=12.0)
        if not result.ok or not result.text:
            continue
        exact_url = getattr(result, "final_url", "") or url
        if _is_aggregator(exact_url):
            continue
        parsed = parser.parse(result.text, exact_url)
        page_identity = " ".join(re.findall(
            r"[a-z0-9]+", (parsed.title + " " + parsed.text_content).lower()))
        if identity not in page_identity:
            continue
        if phone not in {normalize_phone(p) for p in parsed.phones}:
            continue
        # Strip the legal business name before classifying: a registered
        # "North Star Roofing" name alone is not a proven roofing service.
        own_words = " ".join(re.findall(
            r"[a-z0-9]+", " ".join((parsed.title, parsed.description,
                                      *parsed.h1_texts, parsed.text_content[:3000])).lower()))
        trade_text = own_words.replace(identity, " ")
        folded = normalize_trade(trade_text)
        verdict = classifier.classify(description=trade_text, url=exact_url)
        classified = normalize_trade(verdict.get("trade_category", ""))
        if (folded and folded == classified and verdict.get("accepted") and
                verdict.get("confidence", 0) >= 0.6):
            return {"trade": folded, "evidence_url": exact_url,
                    "evidence_kind": "company_website"}
    return empty


def enrich_lead(
    lead: dict[str, Any],
    *,
    overture_fn: Callable[[str], dict[str, str]] | None = None,
    search_fn: Callable[[str, int], list[str]] | None = None,
    fetch_fn: Callable[..., Any] | None = None,
    infer_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Find one phone lead's email; return ``{email, email_source, website,
    dork}``.

    ``email`` is '' when nothing findable — an honest miss, never a guess.
    ``email_source`` says HOW it was found: ``overture`` (the P8 phone-keyed
    dataset join — free, no crawl spent), ``website`` (literally seen on the
    company's own page) or ``pattern_inference`` (the mail server confirmed
    the permutation). ``dork`` tags the emails-vertical feed lane.
    ``website`` is recorded even without an email: it is the provenance of
    the attempt and the input P5's pattern-inference engine needs.
    """
    do_overture = overture_fn or _default_overture
    search = search_fn or _default_search
    do_fetch = fetch_fn or _default_fetch
    do_infer = infer_fn or infer_verified_email
    parser = HTMLParser()

    # Stage 0 (P8): Overture already KNOWS the email behind many numbers —
    # one local-dictionary lookup, and a hit means the search and crawl (and
    # their credits) are never spent on this lead. The furniture rules apply
    # to dataset emails exactly as to scraped ones (§14): a placeholder is
    # a miss, and the pipeline falls through.
    phone = (lead.get("phone") or "").strip()
    if phone:
        hit = do_overture(phone)
        emails = clean_emails([hit.get("email", "")])
        if emails:
            return {
                "email": emails[0],
                "email_source": "overture",
                "website": hit.get("website", ""),
                "dork": "overture",
            }

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
        emails.extend(clean_emails(parsed.emails))
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
