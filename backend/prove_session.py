"""READ-ONLY proof that the SessionManager fix lets HTTPCrawler fetch a real page.

Exercises the exact production path that was failing — SessionManager.session
construction (previously passed the removed `timer` kwarg) followed by
HTTPCrawler._fetch_with_retry. Makes ONE GET request against a host the
earlier diagnostic confirmed is reachable (https://www.abctexas.org, HTTP 200
via requests). No production code is modified.

Usage (from backend/):
    python prove_session.py
"""

from __future__ import annotations

import asyncio
import sys

URL = "https://www.abctexas.org"


async def main() -> int:
    import aiohttp

    from app.crawlers.base import CrawlRequest
    from app.crawlers.http_crawler import HTTPCrawler
    from app.crawlers.session_manager import SessionManager

    print(f"aiohttp version: {aiohttp.__version__}")
    print(f"target         : {URL}")

    # 1-2. ClientSession constructs without the removed `timer` TypeError.
    sm = SessionManager()
    try:
        session = sm.session
        print(
            "[OK] SessionManager.session constructed: "
            f"{type(session).__name__} (closed={session.closed})"
        )
    except TypeError as exc:
        print(f"[FAIL] ClientSession construction raised TypeError: {exc}")
        return 1

    # 3-5. Real HTTP request through the production crawler path.
    try:
        async with HTTPCrawler() as crawler:
            response = await crawler.crawl(
                CrawlRequest(url=URL, timeout=30, respect_robots=True)
            )
    finally:
        await sm.close()

    print(f"      status_code  : {response.status_code}")
    print(f"      successful   : {response.successful}")
    print(f"      bytes        : {len(response.content)}")
    print(f"      content-type : {response.headers.get('Content-Type', '')}")
    print(f"      error        : {response.error!r}")

    ok = (
        response.successful
        and 200 <= response.status_code < 300
        and len(response.content) > 0
    )
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
