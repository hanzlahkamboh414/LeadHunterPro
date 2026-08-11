"""Diagnostic — does the real Tavily API contract match the provider?

Inc10 feeder check. Setting ``TAVILY_SEARCH_API_KEY`` (env or .env) would
auto-register TavilySearchProvider (app/search_providers/__init__.py). This
probe runs the REAL production provider class against the real API — URL
building, HTTP POST, ``resp.json()`` and result parsing all exercised — so
we know the contract is right BEFORE relying on it for live discovery.

Read-only; no production code touched.

PASS  = status "success" and >=1 result with a url/title.
FAIL  = "error" — auth, HTTP, or parse mismatch (an honest answer either way).

Usage:
    python probe_tavily.py                 # uses TAVILY_SEARCH_API_KEY (env/.env)
    python probe_tavily.py tvly-XXXXXXXX    # or pass the key directly
"""

from __future__ import annotations

import asyncio
import sys

from app.search_providers.manager import SearchProviderManager
from app.search_providers.models import SearchQuery
from app.search_providers.tavily import TavilySearchProvider

QUERY = "roofing contractor dallas tx"


async def main(argv: list[str]) -> int:
    key = argv[0] if argv else ""
    if not key:
        from app.core.config import settings

        key = settings.TAVILY_SEARCH_API_KEY
    if not key:
        print("No TAVILY_SEARCH_API_KEY set (pass it as an argument or set it in .env)")
        return 2

    provider = TavilySearchProvider(api_key=key)
    print(f"TavilySearchProvider endpoint={provider._endpoint} enabled={provider.enabled}")

    query = SearchQuery(keywords=QUERY, num_results=15)
    manager = SearchProviderManager()
    # Bypass the singleton so this probe is hermetic.
    manager._registry.clear()
    manager._registry.register(provider)

    response = await manager.search(query)
    print(f"status={response.status!r}  error={response.error[:120]!r}")
    print(f"results={len(response.results)}  latency_ms={response.latency_ms}")

    for i, r in enumerate(response.results[:8], 1):
        print(f"  {i}. {r.title[:60]!r}  {r.url}")
    print("\nPASS" if response.status == "success" and response.results else "\nFAIL")
    await provider.close()  # close the aiohttp session cleanly
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))