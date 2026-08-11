"""Diagnostic — is a search API endpoint reachable from this machine?

TDLR (the real TX roofing registry) is confirmed DNS-blocked at the
machine level (Python AND browser). But probe_seeds showed the machine's
DNS is NOT fully blocked — ABC Texas and ENR resolved (200/403). So a
search-API endpoint may still resolve. This probe checks the candidates
so we don't ask for a Brave/SearXNG key the machine can't even reach.

Read-only; plain requests, no crawler. No production code touched.
"""

from __future__ import annotations

import requests

# Candidate search endpoints a key could be used against.
ENDPOINTS = [
    ("Brave Search API", "https://api.search.brave.com/res/v1/web/search", {"q": "roofing"}),
    ("SearXNG (searx.be)", "https://searx.be/search", {"q": "roofing"}),
    ("SearXNG (paulgo.io)", "https://paulgo.io/search", {"q": "roofing"}),
    ("Google CSE (cse.google.com)", "https://cse.google.com/cse?cx=1", {}),
]


def _probe(name: str, url: str, params: dict) -> None:
    try:
        r = requests.get(url, params=params, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        ok = 200 <= r.status_code < 400
        mark = "OK " if ok else "HT "
        print(f"  [{mark}] {name:<24} {r.status_code}  len={len(r.content)}")
    except requests.exceptions.RequestException as exc:
        print(f"  [--] {name:<24} {type(exc).__name__}: {str(exc)[:80]}")


def main() -> int:
    print("Search-endpoint reachability (does a key have anywhere to go?):")
    for name, url, params in ENDPOINTS:
        _probe(name, url, params)
    print("\nReachable = adding a key to .env has a real endpoint to hit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
