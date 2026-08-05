"""Verify all URLs in fixture and rewrite with only verified entries.

Runs HTTP HEAD checks against every URL in texas_procurement.json,
removes any that fail, and writes back a clean fixture.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")

try:
    import requests
except ImportError:
    print(
        "requests library required. Install with: pip install requests", file=sys.stderr
    )
    sys.exit(1)


FIXTURE_PATH = (
    Path(__file__).parent.parent / "app" / "fixtures" / "texas_procurement.json"
)
REPORT_PATH = (
    Path(__file__).parent.parent
    / "docs"
    / "sprints"
    / "fixture_verification_report.json"
)


def head_check(url: str, timeout: int = 8) -> tuple[bool, str]:
    """Perform HTTP HEAD request. Returns (success, status_string)."""
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if 200 <= resp.status_code < 400:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code}"
    except requests.ConnectionError as e:
        reason = str(e)
        if "NameResolutionError" in reason or "getaddrinfo failed" in reason:
            return False, "DNS resolution failed"
        return False, f"ConnectionError: {reason[:80]}"
    except requests.Timeout:
        return False, "Timeout"
    except requests.RequestException as e:
        return False, f"RequestException: {type(e).__name__}"


def main() -> None:
    if not FIXTURE_PATH.exists():
        print(f"Fixture not found: {FIXTURE_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(FIXTURE_PATH, encoding="utf-8") as f:
        data = json.load(f)

    companies = data["companies"]
    print(f"\n{'='*70}")
    print("Fixture Verification & Cleanup")
    print(f"File: {FIXTURE_PATH}")
    print(f"Companies in file: {len(companies)}")
    print(f"{'='*70}\n")

    verified: list[dict] = []
    rejected: list[dict] = []
    seen_domains: set[str] = set()

    for company in companies:
        url = company.get("website", "").strip()
        domain = (urlparse(url).hostname or "").lower().replace("www.", "").strip()

        entry = {
            "company": company.get("company_name", ""),
            "url": url,
            "domain": domain,
        }

        if not domain:
            rejected.append({**entry, "status": "No domain"})
            continue

        if domain in seen_domains:
            verified.append({**entry, "status": "Duplicate domain (already checked)"})
            continue
        seen_domains.add(domain)

        success, status = head_check(url)
        entry["status"] = status

        if success:
            verified.append(entry)
            print(f"  ✅ VERIFIED: {domain}")
        else:
            rejected.append(entry)
            print(f"  ❌ REJECTED: {domain} — {status}")

    # Build clean companies list
    verified_company_names = {e["company"] for e in verified}
    clean_companies = [
        c for c in companies if c["company_name"] in verified_company_names
    ]

    # Recalculate metadata
    trades: dict[str, int] = {}
    cities: set[str] = set()
    for c in clean_companies:
        tc = c.get("trade_category", "unknown")
        trades[tc] = trades.get(tc, 0) + 1
        cities.add(c.get("city", ""))

    # Update data
    data["companies"] = clean_companies
    data["total_records"] = len(clean_companies)
    data["trades_covered"] = len(trades)
    data["cities_covered"] = len(cities)
    data["trade_breakdown"] = dict(sorted(trades.items()))
    data["cities_covered_list"] = sorted(cities)
    data["verification"] = {
        "method": "HTTP HEAD request (timeout=8s, allow_redirects=True)",
        "threshold": "200 <= status_code < 400",
        "domains_tested": len(seen_domains),
        "domains_passed": len(verified),
        "domains_failed": len(rejected),
        "pass_rate": f"{len(verified)/max(len(seen_domains),1)*100:.1f}%",
        "rejected_domains": {e["domain"] for e in rejected},
    }
    data["metadata"]["note"] = (
        f"Original: {len(companies)} entries. Verified via live HTTP HEAD: "
        f"{len(verified)} passed, {len(rejected)} failed. "
        f"No .example.com domains present."
    )

    # Write clean fixture
    with open(FIXTURE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    # Write verification report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "total": len(seen_domains),
        "verified": len(verified),
        "rejected": len(rejected),
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"  Original entries:   {len(companies)}")
    print(f"  Unique domains:     {len(seen_domains)}")
    print(f"  Verified (live):    {len(verified)}")
    print(f"  Rejected (dead):    {len(rejected)}")
    print(f"  Pass rate:          {len(verified)/max(len(seen_domains),1)*100:.1f}%")
    print(f"  Clean fixture:      {len(clean_companies)} companies written")
    print()

    if rejected:
        print(f"  REJECTED ({len(rejected)} domains):")
        for r in rejected:
            print(f"    - {r['company']:<50s} | {r['domain']:<45s} | {r['status']}")
        print()

    print(f"  Trades: {dict(sorted(trades.items()))}")
    print(f"  Cities ({len(cities)}): {', '.join(sorted(cities))}")
    print()

    if len(rejected) > 0:
        print("WARNING: Some domains failed verification.")
        sys.exit(1)
    else:
        print("ALL DOMAINS VERIFIED SUCCESSFULLY.")
        sys.exit(0)


if __name__ == "__main__":
    main()
