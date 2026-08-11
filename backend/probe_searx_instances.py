"""Diagnostic — which public SearXNG instance still enables the JSON API?

searx.be (the reachable instance) returns text/html for format=json — its
JSON API is disabled, so the production SearXNGProvider cannot parse it.
This probe screens other well-known public instances for the SAME request
shape the provider sends, so we only ask the founder to set SEARXNG_URL
when a real JSON endpoint exists.

Read-only; plain requests; no production code touched.

PASS for an instance = 200 AND Content-Type json AND parses with a
"results" array containing >=1 url.
"""

from __future__ import annotations

import requests

QUERY_PARAMS = {
    "q": "roofing contractor dallas tx",
    "format": "json",
    "categories": "general",
    "safesearch": "1",
    "language": "en",
    "pageno": "1",
    "topics": "general",
}

# Well-known public instances. searx.be included as the confirmed-baseline.
INSTANCES = [
    "https://searx.be",
    "https://searx.tiekoetter.com",
    "https://search.bus-hit.me",
    "https://searxng.site",
    "https://opnxng.org",
    "https://priv.au",
    "https://search.sapti.me",
    "https://baresearch.org",
    "https://searx.perennialte.ch",
    "https://search.hbubli.cc",
]


def _probe(base: str) -> None:
    url = f"{base}/search"
    try:
        r = requests.get(url, params=QUERY_PARAMS, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        ctype = r.headers.get("content-type", "")[:40]
        try:
            data = r.json()
            results = data.get("results", []) if isinstance(data, dict) else []
            ok = "json" in ctype and len(results) > 0
            mark = "OK " if ok else "no "
            n = len(results) if isinstance(results, list) else -1
            print(f"  [{mark}] {base:<32} {r.status_code} ct={ctype:<40} results={n}")
        except ValueError:
            print(f"  [no ] {base:<32} {r.status_code} ct={ctype:<40} HTML (JSON API off)")
    except requests.exceptions.RequestException as exc:
        print(f"  [-- ] {base:<32} {type(exc).__name__}: {str(exc)[:60]}")


def main() -> int:
    print(f"Probing {len(INSTANCES)} public SearXNG instances for format=json support:\n")
    for base in INSTANCES:
        _probe(base)
    print("\n'OK' = SEARXNG_URL=<that base> gives the provider a real JSON endpoint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
