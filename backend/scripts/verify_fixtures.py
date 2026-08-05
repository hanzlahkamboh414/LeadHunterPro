"""Verification script: HTTP HEAD check every domain in the fixture.

Usage:
    python scripts/verify_fixtures.py [path_to_fixture.json]

Prints a verification report:
    ✅ VERIFIED: domain.com
    ❌ REJECTED: dead-domain.com — ConnectionError: ...

Then exits with code 1 if any domains fail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Force UTF-8 for Windows output
sys.stdout.reconfigure(encoding="utf-8")

import requests  # type: ignore[import-not-found]


def extract_domain(url: str) -> str:
    """Extract normalized hostname from URL."""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower().replace("www.", "").strip()


def head_check(url: str, timeout: int = 8) -> tuple[bool, str]:
    """Perform HTTP HEAD request and return (success, status_string)."""
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


def verify_fixture(path: str | Path) -> dict[str, Any]:
    """Verify every URL in a fixture JSON file.

    Args:
        path: Path to the fixture JSON file.

    Returns:
        Report dict with counts, verified list, rejected list.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    companies = data.get("companies", [])
    report = {
        "total": len(companies),
        "verified": [],
        "rejected": [],
    }

    seen_domains: set[str] = set()

    for company in companies:
        url = company.get("website", "").strip()
        domain = extract_domain(url)

        if not domain:
            report["rejected"].append(
                {
                    "company": company.get("company_name", "(no name)"),
                    "url": url,
                    "reason": "No domain found in URL",
                }
            )
            continue

        if domain in seen_domains:
            # Don't re-check same domain
            report["verified"].append(
                {
                    "company": company.get("company_name", ""),
                    "url": url,
                    "domain": domain,
                    "status": "Re-checked (same domain)",
                    "duplicate_of": domain,
                }
            )
            continue

        success, status = head_check(url)
        seen_domains.add(domain)

        entry = {
            "company": company.get("company_name", ""),
            "url": url,
            "domain": domain,
            "status": status,
        }

        if success:
            report["verified"].append(entry)
            print(f"  ✅ VERIFIED: {domain}")
        else:
            report["rejected"].append(entry)
            print(f"  ❌ REJECTED: {domain} — {status}")

    return report


def main() -> None:
    fixture_path = (
        Path(__file__).parent.parent / "app" / "fixtures" / "texas_procurement.json"
    )
    if not fixture_path.exists():
        print(f"Fixture file not found: {fixture_path}", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'='*70}")
    print("FIXTURE VERIFICATION REPORT")
    print(f"File: {fixture_path}")
    print(f"{'='*70}\n")

    report = verify_fixture(fixture_path)

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"  Total companies: {report['total']}")
    print(f"  Verified (live): {len(report['verified'])}")
    print(f"  Rejected (dead): {len(report['rejected'])}")
    print(
        f"  Pass rate:       {len(report['verified'])/max(report['total'],1)*100:.1f}%"
    )
    print()

    if report["rejected"]:
        print(f"  REJECTED COMPANIES ({len(report['rejected'])}):")
        for r in report["rejected"]:
            print(f"    - {r['company']:<45s} | {r['domain']:<45s} | {r['reason']}")
        print()

    # Write report to file for CI
    report_path = (
        Path(__file__).parent.parent
        / "docs"
        / "sprints"
        / "fixture_verification_report.json"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved to: {report_path}")
    print()

    # Exit code: fail if no verified domains
    if len(report["verified"]) == 0:
        print("FATAL: No verified domains found. Fixture is unusable.")
        sys.exit(2)
    elif len(report["rejected"]) > 0:
        print(f"WARNING: {len(report['rejected'])} domain(s) failed verification.")
        sys.exit(1)
    else:
        print("ALL DOMAINS VERIFIED SUCCESSFULLY.")
        sys.exit(0)


if __name__ == "__main__":
    main()
