"""Diagnostic — is TDLR (real TX roofing registry) reachable?

TDLR.texas.gov is the authoritative source of licensed Texas roofing
contractors, but the crawl log shows its robots.txt fetch failing. This
probe fetches both robots.txt and a real page with plain requests (no
crawler) to learn whether the block is network-level (DNS/TLS/status) or
something the crawler does. Read-only; no production code touched.
"""

from __future__ import annotations

import requests


def _fetch(url: str) -> None:
    print(f"\n>>> GET {url}")
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        print(f"    status={r.status_code}  len={len(r.content)}")
        print(f"    final_url={r.url}")
        print(f"    text_preview={r.text[:200]!r}")
    except requests.exceptions.RequestException as exc:
        print(f"    FAILED: {type(exc).__name__}: {exc}")


def main() -> int:
    _fetch("https://www.tdlr.texas.gov/robots.txt")
    _fetch("https://www.tdlr.texas.gov/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
