"""Diagnostic — why did live crawl companies fail the connector filter?

Runs DirectoryCrawlSource directly (the live source that reported
SUCCESS results=3) and prints each raw company record plus the exact
Step-3 filter checks the connector applies, so the drop is explained
field-by-field instead of guessed (CLAUDE.md §7). No production code is
touched; this is a read-only diagnostic.
"""

from __future__ import annotations

from app.connectors.industry_expansion import expand_industry
from app.connectors.texas_procurement import _parse_location
from app.discovery.sources.directory_crawl_source import DirectoryCrawlSource


def main() -> int:
    industry = "Roofing"
    location = "Dallas Texas"
    city, state = _parse_location(location)
    expanded = expand_industry(industry)
    print(f"industry={industry!r} location={location!r}")
    print(f"parsed city={city!r} state={state!r}")
    print(f"expanded_keywords={sorted(expanded)}")
    print("=" * 72)

    status, companies, meta = DirectoryCrawlSource().discover(
        industry=industry, location=location, limit=5
    )
    print(f"source status={status.value} companies={len(companies)}")
    for i, company in enumerate(companies):
        print(f"\n--- company[{i}] ---")
        for key in (
            "company_name",
            "website",
            "city",
            "state",
            "country",
            "trade_category",
            "industry_focus",
            "source_url",
            "data_provenance",
            "_discovery_source",
        ):
            print(f"  {key}: {company.get(key)!r}")
        # Step-3 filter checks (mirror texas_procurement.search)
        name = company.get("company_name", "")
        text = f"{name} {company.get('industry_focus', '')} {company.get('trade_category', '')}".lower()
        match_keyword = any(kw in text for kw in expanded)
        print(f"  >> state check:  {company.get('state','')!r} == {state!r} -> {company.get('state','').upper() == state.upper()}")
        print(f"  >> city check:   {company.get('city','')!r} == {city!r} -> {company.get('city','').lower() == city.lower()}")
        print(f"  >> keyword check {'PASS' if match_keyword else 'FAIL'} (text={text!r})")

    print("\n" + "=" * 72)
    print(f"VERDICT: {len(companies)} crawled -> ", end="")
    kept = 0
    for company in companies:
        name = company.get("company_name", "")
        text = f"{name} {company.get('industry_focus', '')} {company.get('trade_category', '')}".lower()
        state_ok = not state or company.get("state", "").upper() == state.upper()
        city_ok = not city or company.get("city", "").lower() == city.lower()
        if state_ok and city_ok and any(kw in text for kw in expanded):
            kept += 1
    print(f"{kept} survive the Step-3 filter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
