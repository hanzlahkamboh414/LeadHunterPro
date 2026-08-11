"""Diagnostic — does a real SearXNGProvider hit against searx.be work?

Inc9 feeder check. Setting ``SEARXNG_URL=https://searx.be`` would
auto-register SearXNGProvider (app/search_providers/__init__.py:72-100).
This probe runs the REAL production provider class against that instance —
URL building, HTTP, ``resp.json()`` and result parsing all exercised —
so we know whether the JSON API is enabled BEFORE asking the founder to
change .env.

Read-only, plain HTTP, no production code touched.

PASS  = status "success" and >=1 result with a url/title.
FAIL  = "error"/"partial" — searx.be disabled the JSON API (many public
        instances do) and the .env change would produce nothing.
"""

from __future__ import annotations

import asyncio

from app.search_providers.manager import SearchProviderManager
from app.search_providers.models import SearchQuery
from app.search_providers.searxng import SearXNGProvider

BASE_URL = "https://searx.be"


async def main() -> int:
    provider = SearXNGProvider(base_url=BASE_URL)
    print(f"SearXNGProvider({BASE_URL}) enabled={provider.enabled}")

    query = SearchQuery(
        keywords="roofing contractor dallas tx", num_results=15
    )
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

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
