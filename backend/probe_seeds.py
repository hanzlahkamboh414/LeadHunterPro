"""Diagnostic — which planned Texas sources are reachable from this machine?

The SourcePlanner emits TX trade associations (AGC, ABC, THBA), the
Texas Contractor Bulletin directory, and the TDLR licensing board as
company-discovery seeds. The earlier crawl reached ONLY abctexas.org;
TDLR failed DNS. This probe checks every planned seed so we know which
live sources can actually produce companies here before fixing anything
(CLAUDE.md §7 — reachability first, not symptom-patching).

Read-only; plain requests, no crawler. No production code touched.
"""

from __future__ import annotations

import requests

SEEDS = [
    ("TDLR (licensing registry)", "https://www.tdlr.texas.gov/"),
    ("AGC Texas", "https://www.agctexas.org/"),
    ("ABC Texas", "https://www.abctexas.org/"),
    ("THBA", "https://www.thbaonline.com/"),
    ("Texas Contractor Bulletin", "https://www.texascontractor.com/directory"),
    ("ENR Top Specialty", "https://www.enr.com/topspecialtycontractors.aspx"),
]


def _probe(name: str, url: str) -> None:
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
        ok = 200 <= r.status_code < 400
        mark = "OK " if ok else "HT "
        print(f"  [{mark}] {name:<32} {r.status_code}  len={len(r.content)}")
    except requests.exceptions.RequestException as exc:
        kind = type(exc.__name__).__name__ if False else type(exc).__name__
        print(f"  [--] {name:<32} {kind}: {str(exc)[:80]}")


def main() -> int:
    print("Planned TX discovery-source reachability:")
    for name, url in SEEDS:
        _probe(name, url)
    print("\nReachable = can serve as a LIVE company source here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
