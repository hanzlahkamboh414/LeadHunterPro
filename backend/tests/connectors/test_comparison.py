"""Comparison test: Connector vs Discovery Pipeline with full trace.

Queries the connector directly AND runs the full CompanyDiscoveryEngine,
then traces exactly WHY any company present in connector output disappears.
Shows every filtering stage with evidence.
"""

from __future__ import annotations

import sys
from typing import Any

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.connectors.connector_manager import ConnectorManager
from app.connectors.texas_procurement import TexasProcurementConnector
from app.engines.discovery.company.company_cleaner import clean_companies
from app.engines.discovery.company.company_discovery_engine import (
    CompanyDiscoveryEngine,
)
from app.engines.discovery.company.company_models import CompanyDiscoveryResult
from app.engines.discovery.company.company_validator import validate_companies

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
QUERY_INDUSTRY = "Roofing"
QUERY_LOCATION = "Dallas Texas"
QUERY_LIMIT = 20
QUERY_STATE = "TX"
QUERY_CITY = "Dallas"


def extract_domain(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return (parsed.hostname or "").lower().replace("www.", "").strip()


def company_to_dict(cr: Any) -> dict[str, Any]:
    """Convert ConnectorResult or CompanyDiscoveryResult to dict."""
    return {
        "company_name": cr.company_name,
        "website": cr.website,
        "city": getattr(cr, "city", ""),
        "state": getattr(cr, "state", ""),
        "source": getattr(cr, "source", ""),
        "confidence": getattr(cr, "confidence", 0.0),
        "discovery_reason": (
            cr.metadata.get("discovery_reason", "")
            if hasattr(cr, "metadata")
            else cr.discovery_reason
        ),
        "trade_category": (
            cr.metadata.get("trade_category", "") if hasattr(cr, "metadata") else ""
        ),
        "verified_url": (
            cr.metadata.get("verified_url", False) if hasattr(cr, "metadata") else False
        ),
        "source_url": cr.source_url if hasattr(cr, "source_url") else "",
    }


def run_connector_only() -> list[dict[str, Any]]:
    """Step 1: Query connector directly (no validation, no cleaning)."""
    connector = TexasProcurementConnector()
    results, metadata = connector.search(
        industry=QUERY_INDUSTRY,
        location=QUERY_LOCATION,
        limit=QUERY_LIMIT,
    )
    return [company_to_dict(r) for r in results], metadata


def run_full_pipeline() -> tuple[list[dict[str, Any]], list[str]]:
    """Run the full CompanyDiscoveryEngine pipeline."""
    engine = CompanyDiscoveryEngine()
    results, metrics = engine.discover(
        industry=QUERY_INDUSTRY,
        location=QUERY_LOCATION,
        limit=QUERY_LIMIT,
    )
    return [company_to_dict(r) for r in results], [str(e) for e in metrics.errors]


def run_stages_separately() -> dict[str, Any]:
    """Run each pipeline stage separately to show exactly where companies drop off."""
    # Stage A: Connector search
    connector = TexasProcurementConnector()
    conn_results, _ = connector.search(
        industry=QUERY_INDUSTRY,
        location=QUERY_LOCATION,
        limit=QUERY_LIMIT,
    )

    # Stage B: ConnectorManager.deduplicate only (no live validation)
    mgr = ConnectorManager()
    deduped = mgr._deduplicate(list(conn_results))

    # Stage C: Rank
    ranked = mgr._rank_results(list(deduped), QUERY_INDUSTRY, QUERY_LOCATION)

    # Convert to CompanyDiscoveryResult for cleaner/validator
    raw_companies: list[CompanyDiscoveryResult] = []
    for cr in ranked:
        raw_companies.append(
            CompanyDiscoveryResult(
                company_name=cr.company_name,
                website=cr.website,
                city=cr.city,
                state=cr.state,
                country=cr.country,
                source=cr.source,
                confidence=cr.confidence,
                source_url=cr.source_url,
                discovery_reason=cr.metadata.get("discovery_reason", ""),
            )
        )

    # Stage D: Clean (dedup + normalize)
    cleaned, clean_metrics = clean_companies(raw_companies)

    # Stage E: Validate (live URL check)
    validated, val_metrics = validate_companies(cleaned)

    return {
        "stage_a_connector_search": len(conn_results),
        "stage_b_deduplicated": len(deduped),
        "stage_c_ranked": len(ranked),
        "stage_d_cleaned": len(cleaned),
        "stage_e_validated": len(validated),
        "clean_errors": clean_metrics.errors,
        "val_errors": val_metrics.errors,
    }


def main() -> None:
    print()
    print("*" * 80)
    print("*  COMPARISON TEST: Connector vs Full Discovery Pipeline")
    print("*" * 80)
    print(
        f"Query: industry={QUERY_INDUSTRY!r}, location={QUERY_LOCATION!r}, limit={QUERY_LIMIT}"
    )
    print()

    # =========================================================================
    # Stage trace
    # =========================================================================
    print("=" * 80)
    print("PIPELINE STAGE TRACE — Where companies disappear at each stage")
    print("=" * 80)
    stages = run_stages_separately()
    for stage_name, count in [
        ("Stage A: Connector search (raw results)", stages["stage_a_connector_search"]),
        ("Stage B: After deduplication", stages["stage_b_deduplicated"]),
        ("Stage C: After ranking (full set, pre-limit)", stages["stage_c_ranked"]),
        ("Stage D: After clean_companies()", stages["stage_d_cleaned"]),
        (
            "Stage E: After validate_companies() (LIVE URL CHECK)",
            stages["stage_e_validated"],
        ),
    ]:
        print(f"  {stage_name}: {count}")

    if stages["clean_errors"]:
        print(f"\n  Clean errors ({len(stages['clean_errors'])}):")
        for e in stages["clean_errors"][:10]:
            print(f"    - {e}")

    if stages["val_errors"]:
        print(
            f"\n  VALIDATION errors ({len(stages['val_errors'])}) — ALL companies dropped here:"
        )
        for e in stages["val_errors"][:10]:
            print(f"    - {e}")
    print()

    # =========================================================================
    # Connector endpoint results
    # =========================================================================
    conn_companies, conn_meta = run_connector_only()
    conn_domains = {extract_domain(c["website"]) for c in conn_companies}

    print("=" * 80)
    print(f"CONNECTOR ENDPOINT ({len(conn_companies)} results)")
    print("=" * 80)
    print(f"  Data source: {conn_meta.get('data_source', 'N/A')}")
    print(f"  Total in dataset: {conn_meta.get('total_in_dataset', 'N/A')}")
    print(f"  Matched after filters: {conn_meta.get('total_matched', 'N/A')}")
    print(f"  Returned: {conn_meta.get('total_returned', 'N/A')}")
    print()
    for i, c in enumerate(conn_companies, 1):
        domain = extract_domain(c["website"])
        print(f"  {i:2d}. {c['company_name']:<45s} | {domain}")

    # =========================================================================
    # Full discovery pipeline results
    # =========================================================================
    disc_companies, disc_errors = run_full_pipeline()
    disc_domains = {extract_domain(c["website"]) for c in disc_companies}

    print()
    print("=" * 80)
    print(f"FULL DISCOVERY PIPELINE ({len(disc_companies)} results)")
    print("=" * 80)
    if disc_companies:
        for i, c in enumerate(disc_companies, 1):
            domain = extract_domain(c["website"])
            print(f"  {i:2d}. {c['company_name']:<45s} | {domain}")
    else:
        print("  (none returned)")
    if disc_errors:
        print(f"\n  Pipeline errors ({len(disc_errors)}):")
        for e in disc_errors[:10]:
            print(f"    - {e}")
    print()

    # =========================================================================
    # Comparison
    # =========================================================================
    print("=" * 80)
    print("COMPARISON")
    print("=" * 80)
    missing_domains = conn_domains - disc_domains
    missing_companies = [
        c for c in conn_companies if extract_domain(c["website"]) in missing_domains
    ]

    print(f"  Connector endpoint returned:  {len(conn_companies)} companies")
    print(f"  Full pipeline returned:       {len(disc_companies)} companies")
    print(f"  Disappeared in full pipeline: {len(missing_companies)} companies")
    print()

    if missing_companies:
        print("=" * 80)
        print("EVIDENCE: Why each company disappeared")
        print("=" * 80)
        for company in missing_companies:
            domain = extract_domain(company["website"])
            print(f"\n  COMPANY: {company['company_name']}")
            print(f"  Website: {company['website']}")
            print(f"  Domain:  {domain}")
            print(f"  City:    {company['city']}")
            print(f"  Trade:   {company.get('trade_category', 'N/A')}")

            # Check the live HEAD status
            import requests

            try:
                resp = requests.head(
                    company["website"], timeout=5, allow_redirects=True
                )
                head_status = f"HTTP {resp.status_code}"
            except requests.RequestException as e:
                head_status = f"HEAD FAILED: {type(e).__name__}: {str(e)[:100]}"

            print(f"  HEAD check: {head_status}")

            if "example.com" in domain or "example." in domain:
                print(
                    f"  ROOT CAUSE: DEAD FIXTURE URL — '{domain}' is a mock/example domain"
                )
                print(
                    "              that does not exist in DNS. The connector endpoint"
                )
                print(
                    "              skips live URL validation; the full pipeline calls"
                )
                print("              validate_companies() which performs an HTTP HEAD")
                print("              request and drops all non-responsive URLs.")
            else:
                print(
                    "  ROOT CAUSE: Website failed live HEAD check in validate_companies()"
                )

            print()
    else:
        print("  No companies disappeared between connector and discovery endpoints.")
    print()


if __name__ == "__main__":
    main()
