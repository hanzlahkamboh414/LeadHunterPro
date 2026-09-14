"""READ-ONLY live-discovery diagnostic for the crawl pipeline.

Does NOT modify any production code and performs only GET requests.
Reproduces exactly what DirectoryCrawlSource did during the demo run and
reveals the per-seed data the pipeline hides (real HTTP status, redirect
target, TLS/DNS/timeout/connection errors, robots verdict, extractor hit).

Probe A mirrors the CURRENT corrected SessionManager construction
(aiohttp.ClientSession(auto_decompress=False, trust_env=True) — the removed
``timer=None`` kwarg is gone), so aiohttp results reflect the fixed pipeline.

Three probes per seed:
  Probe A — aiohttp, the crawler's exact UA + headers + timeout (8s).
            This is the pipeline path; failures here are the real cause.
  Probe B — requests, a plain python-requests UA. Isolates "site blocks
            the crawler's UA" vs "host genuinely unreachable / blocked".
  robots  — raw robots.txt status + the RobotsManager verdict used in
            production (which is checked with UA "LeadHunterPro-Crawler/1.0").

Each probe is classified into a concrete failure category: HTTP 403/429,
HTTP 5xx, redirect failure, DNS, TLS, timeout, connection error, or
content/decompression problem (raw gzip body with auto_decompress=False).

Usage (run from backend/):
    python diagnose_crawl.py                # Roofing / Dallas Texas
    python diagnose_crawl.py "Roofing" "Dallas Texas"
"""

from __future__ import annotations

import sys
import time
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Live probes (read-only)
# ---------------------------------------------------------------------------

CRAWLER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
PLAIN_UA = "python-requests/2.32"
PROBE_TIMEOUT = 8  # slightly under the crawler's 10s so diagnosis is snappier

_GZIP_MAGIC = b"\x1f\x8b"


def _classify(exc_type: str, status: int | None, is_gzip: bool) -> str:
    """Map a probe outcome to a concrete category for the report."""
    if exc_type:
        t = exc_type
        if t in ("ClientConnectorDNSError", "gaierror"):
            return "DNS failure"
        if t in (
            "ClientSSLError",
            "ClientConnectorSSLError",
            "ClientConnectorCertificateError",
            "SSLError",
            "ssl.SSLError",
        ):
            return "TLS/SSL failure"
        if t in (
            "TimeoutError",
            "asyncio.TimeoutError",
            "Timeout",
            "ConnectTimeout",
            "ReadTimeout",
            "ServerTimeoutError",
            "ConnectionTimeoutError",
            "SocketTimeoutError",
        ):
            return "timeout"
        if t in (
            "TooManyRedirects",
            "InvalidUrlRedirectClientError",
            "NonHttpUrlRedirectClientError",
        ):
            return "redirect failure"
        if t in ("ContentTypeError", "ClientPayloadError", "UnicodeDecodeError"):
            return "content/decode problem"
        if t in (
            "ClientConnectorError",
            "ClientConnectionError",
            "ClientOSError",
            "ClientConnectionResetError",
            "ConnectionResetError",
            "ServerDisconnectedError",
            "ConnectionError",
            "OSError",
        ):
            return "connection error"
        if t == "ClientResponseError":
            return f"HTTP error (status={status})"
        if t == "InvalidURL":
            return "invalid URL"
        return f"other ({t})"
    if status is not None:
        if status == 403:
            return "HTTP 403 — blocked"
        if status == 429:
            return "HTTP 429 — rate-limited"
        if 500 <= status < 600:
            return f"HTTP {status} — 5xx server error"
        if 300 <= status < 400:
            return f"HTTP {status} — redirect (unexpected)"
        if 200 <= status < 300:
            if is_gzip:
                return "OK — but raw gzip body (auto_decompress=False, unusable in production)"
            return f"OK HTTP {status}"
        return f"HTTP {status}"
    return "unknown"


def probe_aiohttp(url: str) -> dict:
    """Fetch *url* exactly like HTTPCrawler._fetch_with_retry does.

    Uses the corrected SessionManager construction (no ``timer`` kwarg).
    """
    import aiohttp

    headers = {
        "User-Agent": CRAWLER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    start = time.monotonic()
    try:
        async def _run() -> dict:
            async with aiohttp.ClientSession(
                auto_decompress=False, trust_env=True
            ) as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT),
                    allow_redirects=True,
                ) as resp:
                    content = await resp.read()
                    is_gzip = content[:2] == _GZIP_MAGIC
                    return {
                        "ok": True,
                        "status": resp.status,
                        "final_url": str(resp.url),
                        "bytes": len(content),
                        "elapsed": round(time.monotonic() - start, 2),
                        "content_type": resp.headers.get("Content-Type", ""),
                        "html": len(content) > 0,
                        "is_gzip": is_gzip,
                        "category": _classify("", resp.status, is_gzip),
                        "snippet": content[:80].decode("utf-8", "replace") if content else "",
                        "error": "",
                    }

        import asyncio

        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": None,
            "final_url": url,
            "bytes": 0,
            "elapsed": round(time.monotonic() - start, 2),
            "content_type": "",
            "html": False,
            "is_gzip": False,
            "category": _classify(type(exc).__name__, None, False),
            "snippet": "",
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def probe_requests(url: str) -> dict:
    """Independent reachability check with a plain UA."""
    import requests

    start = time.monotonic()
    try:
        r = requests.get(
            url,
            headers={"User-Agent": PLAIN_UA},
            timeout=PROBE_TIMEOUT,
            allow_redirects=True,
        )
        is_gzip = r.content[:2] == _GZIP_MAGIC
        looks_html = r.text.lstrip().lower().startswith(("<html", "<!doctype", "<"))
        return {
            "ok": 200 <= r.status_code < 300,
            "status": r.status_code,
            "final_url": r.url,
            "bytes": len(r.content),
            "text": r.text,
            "looks_html": looks_html,
            "is_gzip": is_gzip,
            "category": _classify("", r.status_code, is_gzip),
            "elapsed": round(time.monotonic() - start, 2),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        category = _classify(type(exc).__name__, None, False)
        if "getaddrinfo" in str(exc).lower() or "dns" in str(exc).lower():
            category = "DNS failure"
        return {
            "ok": False,
            "status": None,
            "final_url": url,
            "bytes": 0,
            "text": "",
            "looks_html": False,
            "is_gzip": False,
            "category": category,
            "elapsed": round(time.monotonic() - start, 2),
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
        }


def probe_robots(host: str) -> dict:
    """Fetch robots.txt and run the production RobotsManager verdict."""
    from app.crawlers.cache import ResponseCache
    from app.crawlers.robots import RobotsManager

    robots_url = f"https://{host}/robots.txt"
    manager = RobotsManager(cache=ResponseCache())

    # Raw robots.txt status
    import requests

    status, snippet = None, ""
    try:
        r = requests.get(robots_url, headers={"User-Agent": CRAWLER_UA}, timeout=PROBE_TIMEOUT)
        status = r.status_code
        snippet = r.text[:120].replace("\n", " | ")
    except Exception as exc:  # noqa: BLE001
        status = f"{type(exc).__name__}"

    # The production verdict (called with the default crawler UA, not CRAWLER_UA)
    import asyncio

    try:
        directive = asyncio.run(manager.is_allowed(robots_url))
        verdict = f"ALLOWED ({directive.reason})" if directive.allowed else f"BLOCKED ({directive.reason})"
    except Exception as exc:  # noqa: BLE001
        verdict = f"check-error {type(exc).__name__}"
    return {"robots_url": robots_url, "status": status, "snippet": snippet, "verdict": verdict}


def extractor_hit(url: str, html: str, industry: str) -> tuple[int, list[str]]:
    """Run the real CompanyExtractor on fetched HTML."""
    from app.search_providers.company_extractor import CompanyExtractor

    try:
        profiles = CompanyExtractor().extract(
            url, html, title="", description="", context={"industry_hint": industry}
        )
        return (1, [profiles.name]) if profiles.name else (0, [])
    except Exception as exc:  # noqa: BLE001
        return (0, [f"extractor-error {type(exc).__name__}"])


# ---------------------------------------------------------------------------
# Seed set — identical to DirectoryCrawlSource's selection
# ---------------------------------------------------------------------------


def get_seeds(industry: str, location: str) -> tuple[list, list]:
    from app.connectors.texas_procurement import _parse_location
    from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource
    from app.engines.source_intelligence.source_models import SourcePlannerRequest
    from app.engines.source_intelligence.source_planner import SourcePlanner

    city, state = _parse_location(location)
    planned = SourcePlanner().plan(
        SourcePlannerRequest(industry=industry, country="USA", state=state, city=city)
    )
    source = DirectoryCrawlSource()
    crawlable = [
        r for r in planned.sources if r.supports_company_discovery and r.url
    ]
    selected = source._select_seeds(planned.sources, state=state)
    return crawlable, selected


def main() -> int:
    industry = sys.argv[1] if len(sys.argv) > 1 else "Roofing"
    location = sys.argv[2] if len(sys.argv) > 2 else "Dallas Texas"
    print("=" * 78)
    print("LIVE DISCOVERY DIAGNOSTIC (read-only) — industry=%r location=%r" % (industry, location))
    print("=" * 78)

    crawlable, selected = get_seeds(industry, location)
    selected_urls = {s.url for s in selected}
    print(f"\nCrawlable seeds: {len(crawlable)}  |  attempted (MAX_SEEDS={len(selected)}):")
    for s in crawlable:
        marker = "  << ATTEMPTED" if s.url in selected_urls else ""
        print(f"  {'[X]' if s.url in selected_urls else '[ ]'} {s.name:<45} {s.url}{marker}")

    failures = 0
    successes = 0
    categories: dict[str, int] = {}
    print("\n" + "=" * 78)
    print("PER-SEED PROBES")
    print("=" * 78)
    for seed in crawlable:
        url = seed.url
        host = (urlparse(url).netloc or "").lower().removeprefix("www.")
        attempted = url in selected_urls
        tag = "ATTEMPTED" if attempted else "not-attempted"
        print(f"\n--- {seed.name} [{tag}]")
        print(f"    {url}  (state={seed.state or 'national'}, priority={seed.priority})")

        rob = probe_robots(host)
        print(f"    robots.txt : HTTP {rob['status']}  |  production verdict: {rob['verdict']}")
        if rob["snippet"]:
            print(f"                 snippet: {rob['snippet']}")

        a = probe_aiohttp(url)
        if a["ok"]:
            print(f"    aiohttp    : HTTP {a['status']} -> {a['final_url']} "
                  f"({a['bytes']} bytes, {a['elapsed']}s) ct={a['content_type'][:30]}")
            if a["is_gzip"]:
                print("                 !! RAW GZIP body detected — production receives unusable "
                      "compressed bytes (auto_decompress=False + gzip Accept-Encoding)")
        else:
            print(f"    aiohttp    : FAILED [{a['error']}] ({a['elapsed']}s)")
            print("                 (pipeline collapses this to status_code=0, successful=False)")
        print(f"    aiohttp    : {a['category']}")

        b = probe_requests(url)
        if b["ok"]:
            print(f"    requests   : HTTP {b['status']} -> {b['final_url']} "
                  f"({b['bytes']} bytes, {b['elapsed']}s) html={b['looks_html']}")
        else:
            print(f"    requests   : FAILED [{b['error']}]")
        print(f"    requests   : {b['category']}")

        if attempted:
            if a["ok"]:
                successes += 1
                html = b.get("text", "")
                if html:
                    _hit, names = extractor_hit(url, html, industry)
                    if names:
                        print(f"    extractor  : CompanyExtractor found -> {names}")
                    else:
                        print(
                            "    extractor  : CompanyExtractor found no company "
                            "(page may not be a member profile)"
                        )
                verdict = "SUCCESS/EMPTY (fetch ok; companies depend on page content)"
            else:
                failures += 1
                verdict = "UNAVAILABLE (seed fetch failed in the pipeline)"
            print(f"    => pipeline verdict for this seed: {verdict}")

        # Per-seed diagnosis (category + gzip annotation)
        a_diag = a["category"]
        if a["ok"]:
            if a["is_gzip"]:
                seed_diag = "FETCHED but content unusable in production (raw gzip body)"
                categories["ok-but-gzip (decompress defect)"] = (
                    categories.get("ok-but-gzip (decompress defect)", 0) + 1
                )
            else:
                seed_diag = f"FETCHED OK (HTTP {a['status']})"
                categories["ok"] = categories.get("ok", 0) + 1
        else:
            seed_diag = f"FETCH FAILED — {a_diag}"
            categories[a_diag] = categories.get(a_diag, 0) + 1
        if rob["verdict"].startswith("BLOCKED"):
            seed_diag += "  |  robots BLOCKED"
        print(f"    => DIAGNOSIS: {seed_diag}")

    print("\n" + "=" * 78)
    print(f"ATTEMPTED SEEDS: {len(selected)}  |  fetch-ok: {successes}  |  fetch-failed: {failures}")
    print("FAILURE CATEGORY BREAKDOWN (aiohttp / pipeline path):")
    for key, count in sorted(categories.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3}  {key}")
    if failures and successes == 0:
        print("PIPELINE VERDICT: UNAVAILABLE — every attempted seed fetch failed (matches demo run)")
    elif failures and successes:
        print("PIPELINE VERDICT: would have been SUCCESS — at least one seed fetched")
    elif not failures:
        print("PIPELINE VERDICT: UNAVAILABLE is NOT explained by reachability — "
              "investigate robots/TLS/proxy/cache interaction")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
