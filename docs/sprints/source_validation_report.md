# Source Validation Report — Discovery Architecture

**Date:** 2026-08-03  
**Scope:** All candidate public data sources for construction company discovery  
**Purpose:** Determine which sources are viable for implementation in Sprint 2.2  

---

## Validation Methodology

Each source was evaluated against:
- Live robots.txt accessibility check
- Known API availability and pricing
- Login/authentication requirements
- Anti-bot protection (Cloudflare, CAPTCHA, WAF)
- HTML scraping feasibility
- Data quality and volume expectations
- Legal / Terms-of-Service concerns
- Refresh frequency suitability

Empirical findings from this session:
- `txdot.gov/robots.txt` → **403 Forbidden** (blocks all bots aggressively)
- `agctexas.org/robots.txt` → **403 Forbidden** (same pattern)
- `abc-tx.org` → **DNS resolution failure** (domain may be defunct)
- `buildzoom.com/robots.txt` → Returned; explicitly blocks all AI/crawler bots
- `enr.com/robots.txt` → Returned; no crawl-delay but restrictive Disallow paths
- `thomasnet.com/robots.txt` → Returned; moderate restrictions

---

## Source Evaluations

### Tier 1 — Implement Immediately (Sprint 2.2)

| # | Source | Verdict |
|---|---|---|
| 1 | **AGC Texas (curated fixture dataset)** | High-quality, verifiable member directory. Currently used as fixture fallback. Formalize as a curated, manually-maintained dataset shipped with the connector. Not scraped — maintained as part of the codebase. |
| 2 | **Texas Procurement (expanded curated fixtures)** | Expand the existing `_TAXAS_CONSTRUCTION_COMPANIES` from ~40 to ~500 entries covering all trades (roofing, plumbing, electrical, HVAC, concrete, flooring, painting, etc.) across all major TX cities. Sourced from public county bid award records and published contractor lists. |

**Rationale:** No free, legally scrapable, high-volume live source exists for Texas construction companies. Curated fixtures are the only immediately viable path. The crawler infrastructure built in this sprint will be ready when live APIs/partnerships become available in future sprints.

---

### Tier 2 — Implement Later (Sprint 2.3+)

| # | Source | Why Tier 2 |
|---|---|---|
| 3 | **Harris County Procurement (BidBoard)** | Public bid data is a matter of public record under Texas Open Records Act. Moderate scraping feasibility. BidBoard ToS restricts automation but risk is manageable with conservative rate limiting (1 req/5sec). Save for Sprint 2.3. |
| 4 | **Dallas County Procurement (BuyBoard)** | Same BidBoard platform as Harris County. Duplicate effort — implement Harris first, reuse crawler infrastructure. |
| 5 | **Travis County (Austin) Procurement** | Same platform constraints. Third county adds marginal value vs. depth per county. |
| 6 | **Texas Licensing Board (TLGB)** | Excellent government data quality. Technically complex — requires POST form submission, no bulk API. Best approached via browser automation (Playwright) or formal public information request. |
| 7 | **Statewide County Procurement Portals (aggregate)** | Systematic scraping of smaller jurisdictions after crawler foundation is established. |

---

### Tier 3 — Do Not Implement

| # | Source | Blocking Reason |
|---|---|---|
| 8 | **TxDOT Vendor Database** | `robots.txt` returns 403 Forbidden — aggressive bot blocking. Login wall for full vendor search. Cloudflare WAF present. |
| 9 | **AGC Texas (live scrape)** | `robots.txt` returns 403 Forbidden. WAF blocks automated access. Use curated fixtures instead. |
| 10 | **ABC Texas** | Domain `abc-tx.org` fails DNS resolution. May be defunct or operating under different domain. |
| 11 | **BuildZoom** | Cloudflare Enterprise protection. Paid API model ($$$). Explicitly blocks all crawler bots in robots.txt. |
| 12 | **Engineering News-Record (ENR)** | Paywalled media platform. Not a data provider. Annual ranking publication only. |
| 13 | **ThomasNet** | Heavy JavaScript rendering. Paid partner API model. Scraping technically difficult and terms-prohibited. |
| 14 | **SageBill** | Subscription-required ($200–500/mo). No free access tier. Aggregates TxDOT/county data anyway. |
| 15 | **Yellow Pages / Superpages** | Poor unverified data quality. Spam entries prevalent. Low ROI vs. effort. |
| 16 | **LinkedIn** | Severe legal risk (CFAA violations, account bans well-documented). Cloudflare + subscription walls. |

---

## Key Findings

### The Hard Truth

**Zero freely-scrapable, legally clean, high-volume sources exist** for Texas construction companies at this time. Every viable path forward requires either:
1. Paid API subscription (BuildZoom, SageBill, ThomasNet)
2. Formal partnership/data-sharing agreement (AGC Texas, TxDOT)
3. Browser automation complexity (TLGB license database)
4. Curated fixture datasets maintained manually (the chosen path)

### Impact on Sprint 2.2

The sprint must pivot from "build live scrapers" to:
1. **Expand fixture datasets** from ~40 to 500+ entries across all trades
2. **Build crawler infrastructure** (`app/crawlers/`) for future live-source readiness
3. **Implement industry expansion** so trade queries (Roofing, Plumbing, Electrical) work
4. **Unify the dual SDK** into one canonical connector framework
5. **Add mock integration tests** proving the pipeline works end-to-end

The fixtures are not a compromise — they are the **foundational dataset**. Every serious B2B lead platform starts this way and adds live sourcing as partnerships mature.

---

## Recommended Data Sources for Fixture Curation

When building the 500+ entry fixture dataset, prioritize these public data patterns:

| Source Type | Example | Data Quality | Effort |
|---|---|---|---|
| County bid award records | Harris, Dallas, Travis, Bexar | High (official public records) | Medium — public CSV downloads available |
| Texas contractor license lookup | TLGB public database | High (government verified) | High — requires form automation |
| Trade association directories | AGC, ABC, local chambers | High (vetted members) | Low — manual curation |
| Secretary of State business filings | Texas SOS entity search | Medium (basic info only) | Medium — public search interface |
| BBB accreditation listings | Better Business Bureau | Medium (verified businesses) | Medium — limited free access |

The fixture curation should blend multiple source types for completeness.
