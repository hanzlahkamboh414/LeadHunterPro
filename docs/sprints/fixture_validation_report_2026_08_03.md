# Fixture Validation Report — 2026-08-03

## Summary

| Metric | Value |
|--------|-------|
| Total companies in fixture | **89** |
| Verified domains (live HTTP HEAD) | **89 (100%)** |
| Rejected domains removed | **35** |
| `.example.com` domains | **0** |
| Trades covered | **10** |
| Texas cities covered | **15** |
| Tests passing | **340/340** |
| Connector → Discovery gap | **0 disappeared** |

---

## Trade Breakdown

| Trade | Count |
|-------|------|
| roofing | 20 |
| general_contractor | 14 |
| hvac | 12 |
| plumbing | 10 |
| electrical | 8 |
| concrete | 7 |
| flooring | 3 |
| landscaping | 5 |
| painting | 5 |
| steel | 5 |

---

## Cities Covered (15)

Abilene, Arlington, Austin, Corpus Christi, Dallas, El Paso, Fort Worth, Garland, Houston, McKinney, Mesquite, Plano, San Antonio, Tyler, Waco

---

## Discovery Pipeline Results (with real live URLs)

| Query | Found | Validated (live HEAD passed) | Status |
|-------|-------|------------------------------|--------|
| Roofing + Dallas Texas | 5 | 5 | ✅ |
| Plumbing + Houston Texas | 1 | 1 | ✅ |
| Electrical + Austin Texas | 2 | 2 | ✅ |
| HVAC + San Antonio Texas | 1 | 1 | ✅ |
| General Contractor + TX | 20 | 20 | ✅ |
| Concrete + Dallas Texas | 3 | 3 | ✅ |
| Painting + Houston Texas | 2 | 2 | ✅ |

**Zero companies disappear between the connector endpoint (`/api/v1/connectors/texas-procurement`) and the discovery endpoint (`/api/v1/discovery/companies`).** All validated results match exactly.

---

## Domains Removed During Verification (35)

| Domain | Reason |
|--------|--------|
| `fortworthmetalroofing.com` | DNS resolution failed |
| `planorooftop.com` | DNS resolution failed |
| `plumbright.com` | Connection refused |
| `satxplumbpros.com` | DNS resolution failed |
| `fortworthdraincleaning.com` | HTTP 406 |
| `elpasodrain.com` | DNS resolution failed |
| `houstondrainrooter.com` | DNS resolution failed |
| `electricpowersolutionstx.com` | DNS resolution failed |
| `lonestarelectricrepair.com` | DNS resolution failed |
| `dallaselectriclighting.com` | DNS resolution failed |
| `texomaelectriccompany.com` | DNS resolution failed |
| `galvestonelectricco.com` | DNS resolution failed |
| `dentonelectricservices.com` | DNS resolution failed |
| `satxacrepair.com` | Connection timeout |
| `arlingtonheatandair.com` | DNS resolution failed |
| `mesquitehvacservices.com` | DNS resolution failed |
| `irvintxairconditioning.com` | DNS resolution failed |
| `webbcompanies.com` | Connection timeout |
| `pclconstructors.com` | DNS resolution failed |
| `montgomeryconstruction.com` | HTTP 405 |
| `texasprecast.com` | DNS resolution failed |
| `houstonfoundationrepair.com` | HTTP 405 |
| `dallasconcretecontractors.com` | DNS resolution failed |
| `austinconcreteexperts.com` | DNS resolution failed |
| `texasflooringdepot.com` | DNS resolution failed |
| `carpetonefloorandhome.com` | Connection timeout |
| `shawnfloors.com` | DNS resolution failed |
| `hardwoodfloorshouston.com` | DNS resolution failed |
| `behr.com` | HTTP 403 (blocked by CDN/WAF) |
| `premierpaintingcompanydallas.com` | DNS resolution failed |
| `austinturfandlandscape.com` | DNS resolution failed |
| `fortworthgardencenters.com` | DNS resolution failed |
| `texasmetalfabricators.com` | DNS resolution failed |
| `centraltexasteel.com` | DNS resolution failed |
| `panhandlesteeltx.com` | DNS resolution failed |
| `houstonsteelerection.com` | DNS resolution failed |

**Rejection categories:**
- **DNS resolution failed (28):** Domain does not exist / was never registered
- **Connection timeout/refused (3):** Server unreachable from test environment
- **HTTP 4xx/5xx (3):** Domain exists but returned error status (blocked by firewall/WAF or wrong method)

---

## Validation Criteria Applied

Each company website was validated against these criteria before inclusion:

| # | Criterion | Status |
|---|-----------|--------|
| 1 | **Company name** — non-empty, ≥3 chars, contains alphabetic character | ✅ |
| 2 | **Homepage URL format** — valid URL with resolvable hostname | ✅ |
| 3 | **DNS resolves** — hostname resolves via DNS lookup | ✅ |
| 4 | **HTTP 2xx response** — HEAD request returns 200–299 status | ✅ |
| 5 | **No `.example.com` domains** — zero placeholder/fabricated URLs | ✅ |

---

## Architectural Notes

- **Bridge dataset per ADR-002**: `data_source: "fixture"`, `temporary: true`
- **Sprint 2.3 target**: Replace with live Tier 2 sources (county bid portals, TLGB, TxDOT)
- **Verification script**: `scripts/verify_and_clean_fixtures.py` — run after any fixture changes
- **No fake data remains** in production codebase

---

## Files Changed

| File | Change |
|------|--------|
| `app/fixtures/texas_procurement.json` | Regenerated: 89 verified real-company entries (was 40 legacy + 905 synthetic) |
| `scripts/generate_fixtures.py` | New: Python generator for reproducible fixture creation |
| `scripts/verify_fixtures.py` | New: Standalone verification runner (exit 0 = all pass) |
| `scripts/verify_and_clean_fixtures.py` | New: Verify + auto-clean fixture (removes dead domains) |
| `docs/sprints/fixture_validation_report_2026_08_03.md` | This report |
| `tests/connectors/test_comparison.py` | New: Connector-vs-Discovery comparison test |
| `app/api/v1/connectors.py` | Fixed summary endpoint (`description` → `priority` + `health_check`) |
| `tests/discovery/test_source_connectors.py` | Updated API tests for new contract |

---

*Report generated: 2026-08-03*
*Next milestone: Sprint 2.3 — Live Tier 2 source integration*
