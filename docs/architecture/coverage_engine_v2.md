# Coverage Engine V2 — Source-Adapter Architecture

**Date:** 2026-09-17  
**Status:** Approved design — implementation is phase-gated (one phase per approval)
**Builds on:** founder rule "AI for judgment, seed lists for enumeration"; check.txt blueprint; the 8-point bottleneck review.

---

## 0. Why this exists (root cause, one paragraph)

The V1 scout was asked to *discover* coverage: "find me new datasets." Socrata-only crawling returned a finite pool (10 candidates), the AI promoted 2 (OR, VT), the pool exhausted, and the proposal stage became a permanent no-op — while 46 states sat at zero coverage. Meanwhile CSLB (CA) was marked `dead` for a **403/F5-WAF block** that a bulk-file access path would have walked around, TX `mechanical` burned 10 trials fetching rows that had no phone field, and demand rows with `trade=''` were structurally unreachable by the emails lane. The fix is not "smarter AI" — it is **deterministic seed lists + capability-aware routing**, with AI confined to the one job it is good at: writing adapters.

---

## 1. Architecture at a glance

```
 SEED LISTS (deterministic, committed data)
   phones:  state_license_boards.csv (50 states × agencies)
   emails:  email_sources.csv         (overture, common_crawl,
                                       email-bearing boards, directories)
        │
        ▼
 ACCESS PROBER  (plain Python, NO AI — 5 paths, priority order)
   bulk_file ▸ open_data_api ▸ xhr_json ▸ html_form ▸ pdf
        │  first path that works is recorded; on 403/429 → next path
        ▼
 ADAPTER WRITER (AI — the ONLY AI step)
   field_map + trade_mapping + capabilities + fetch spec + file_shape
        │
        ▼
 VALIDATOR (plain Python, hard rules)
   gate 0 parse-feasibility → 5 semantic gates → re-measure estimated_rows
        │
        ▼
 PROBATION  (N-row dry run, verdicts accumulate)
        │
        ▼
 PROMOTE  ──▶ ROUTER (capability-matched demand routing)
        │
        ├─ blocked (403/captcha) → retry next access path
        ├─ schema_mismatch      → capability downgrade, retry
        ├─ dead (404/410 only)  → permanent skip
        └─ exhausted (dup>90%)  → 30-day re-arm
```

**Invariant:** candidate pool can never be empty. 50 fixed state rows × up to 5 access paths each = ≥250 live slots, plus the email seed. A state stays in the queue until it reaches `promoted` or `dead` (true 404 only).

---

## 2. Seed lists (the fix for "input list khali thi")

Two committed CSV files under `backend/app/source_scout/seeds/`, versioned, with a schema test that asserts no duplicate states, a known vocabulary, and no empty rows.

### 2.1 `state_license_boards.csv` — phones/address/license domain

| col | example |
|---|---|
| state | CA |
| agency_code | CSLB |
| agency_name | Contractors State License Board |
| base_url | https://www.cslb.ca.gov |
| trade_scope | all_trades (or enumerated list) |
| priority_rank | 1..50 — Census/BLS construction-value-by-state; highest spend = 1 |
| known_access | bulk_file (verified hint, can be empty) |
| notes | monthly master list publishes; record hash dedupe |

`trade_scope` is mandatory — TX's lesson: not every state licenses every trade. TX has no state-level general-contractor license, so the router must never bind `general contractor × TX` to TDLR.

### 2.2 `email_sources.csv` — email domain

Same rigor as the phone seed — **every row is a named, real source; categories are not rows:**

| col | example |
|---|---|
| source_id | overture_places |
| access_path | bulk_file / open_data_api |
| base_url | https://overturemaps.org |
| capabilities_hint | {email: {present: true}, phone: {present: true}} |
| estimated_rows | 74,000,000 — verify exact CDLA endpoint + count at build |
| notes | free gov-style data license; places + contacts |

Committed rows at build: `overture_places`, `common_crawl` (WARC/WAT snapshot via https://index.commoncrawl.org, `estimated_rows` TBD at build). **"email-bearing boards" / "directories" are fill tasks for Phase 5, not placeholder rows** — a category stays out until it has a real URL + source + count. Boards mostly do **not** publish emails — that is the whole reason emails need their own seed tier. See §8.

### 2.3 Governance

- Committed data + `tests/source_scout/test_seed_lists.py` — phones: row count ≥ 45, unique state, **unique `priority_rank` covering 1..N**, `trade_scope` ⊆ CANONICAL_TRADES vocabulary; emails: ≥ 3 real named rows, each with `base_url` + `estimated_rows`, **no category-only rows**.
- Seed refinements are PRs, not runtime AI edits.

---

## 3. Source registry — DB schema

Migrate the scout store (`source_scout.db → sources` table) by adding columns; old `known_sources`/`verdicts` tables stay. New shape:

```
source_id        TEXT PK        — lowercase slug, e.g. ca_cslb_bulk
seed_domain      TEXT           — phones | emails | both
state            TEXT           — authoritative jurisdiction (source_state)
name, base_url   TEXT
access_path      TEXT           — bulk_file|open_data_api|xhr_json|html_form|pdf
fetch            JSON           — {method,url,format,refresh,if_modified,etag,file_shape}
field_map        JSON           — canonical → source column  (AI-written, validated)
trade_mapping    JSON           — source codes → canonical slugs (AI-written)
capabilities     JSON           — per capability {present:bool, fill_rate:float}, e.g.
                                   {"phone": {"present": true, "fill_rate": 0.62}, "email":
                                    {"present": false, "fill_rate": 0.04}, ...}
                                   present  → router eligibility (quantity)
                                   fill_rate → quota formula + export/CRM priority (quality)
                                   SET at promotion, fill_rate re-measured each probation
estimated_rows   INT            — validator re-measured during probation, never AI-only
rows_consumed    INT            — stock counter driving quota ceiling + exhaustion
status           TEXT           — untried|probing|adapter_draft|probation|promoted|
                                   exhausted|blocked|dead
next_retry_at    TEXT           — 30-day re-arm for exhausted/blocked_all_paths
last_fetched_at, last_verified_at TEXT
gate_fail_reason TEXT           — honest specific reason on every fail
```

**Two-state split for correctness (ID-phantom fix):** every sourced row stores
`license_jurisdiction` (= the record's `state_field`, authoritative source value)
**and** `parsed_address_state` (inferred from address string, display-only).
Routing, reporting, and dedupe key on `license_jurisdiction` only.

**Entity-Resolution integration (note, no new code):** every sourced row (`company_name` + `address` + `state`) feeds the existing ER layer **before storage**, so the same company arriving from CSLB then Overture resolves to **one** dossier → one outreach record. Skip this and outreach double-hits one company — sender-reputation damage, not reply-rate.

---

## 4. Access Prober (deterministic, no AI)

For a given state row, walk the 5 paths in priority order; record the first that returns data.

| # | Path | Detection | Quality |
|---|---|---|---|
| 1 | **Bulk file** (CSV/ZIP/TXT) | site crawl for "download / data files / public records / bulk"; Serper/Tavily dork `site:agency.gov (download OR "data file")` | best — no rate-limit, no WAF block |
| 2 | **Open-data API** | Socrata catalog API / CKAN + ArcGIS Hub discovery (paginated) | best |
| 3 | **JSON/XHR endpoint** | portal search page's own network call | good |
| 4 | **HTML form** | form POST + pagination | slow, block-prone |
| 5 | **PDF roster** | `filetype:pdf` dork | last resort |

Rules:
- `403/429/captcha` = **blocked**, never dead → record reason, try the *next* path (CSLB: path 3/4 WAF-blocked → batch file path 1 is the win).
- `404/410` = **dead** (permanent skip).
- Portal scraping is *always* the last option, never the first. Public-records law means most agencies also publish a download form.

---

## 5. Adapter Writer (AI — the one judgment step)

The AI returns ONE standard contract per candidate. Every field is a **real column sampled from the source**, validated against the actual file/API:

```json
{
  "source_id": "ca_cslb_bulk",
  "state": "CA",
  "access_path": "bulk_file",
  "fetch": {
    "method": "GET", "url": "https://www.cslb.ca.gov/.../master.zip",
    "format": "zip/csv", "refresh": "monthly",
    "if_modified": { "etag": null, "last_modified": null },
    "file_shape": { "inner_path": "MasterList.csv", "delimiter": ",",
                    "encoding": "cp1252", "header_row": 1, "zip_bomb_max_mb": 500 }
  },
  "field_map": { "company_name": "BusinessName", "phone": "BusinessPhone",
                 "email": null, "address": "MailingAddress", "city": "City",
                 "state_field": "State", "license_no": "LicenseNo",
                 "trade": "ClassificationCode", "status": "PrimaryStatus" },
  "capabilities": { "phone": {"present": true}, "email": {"present": false},
                    "address": {"present": true} },
  "trade_mapping": { "B": "general contractor", "C-10": "electrician" },
  "estimated_rows": 280000
}
```

Notable: `file_shape` (parse mechanics) is the half that keeps adapters from rotting — encoding/delimiter/ZIP inner path must be explicit. The adapter carries **`present` flags only, never numbers** — the numeric `fill_rate` is the Validator's job to MEASURE (§6) and the registry stores `{present, fill_rate}` at promotion. `present:false` is the permanent fix for TX-mechanical (no phone → router never binds phone demand to it; yield-learning is then economics, not schema papering).

---

## 6. Validator (plain Python, hard rules)

Before promotion: a real N-row dry run through the (parseable) adapter.

```
gate 0  parse_feasibility   — the file/API actually yields typed rows
gate 1  rows_returned >= 50 — else empty_source
gate 2  company_name fill >= 95% — else schema_mismatch (field_map wrong)
gate 3  per-claimed-capability fill_rate >= 0.30 — else downgrade THAT capability,
        never reject the whole source; the measured fill_rate is STORED in
        capabilities[n].fill_rate — the number §8's quota formula requires
gate 4  state_field ≡ seed state — else jurisdiction_error (kills ID phantoms)
gate 5  duplicate rate < 20% — else pagination/file dedupe broken
```

**Re-measure `estimated_rows`** — for open-data: `$select=count(*)`; for files: line count. The AI estimate becomes an input, the validator's count is the truth that feeds the quota ceiling. Fail = *specific recorded reason*, retryable except 404s.

**Deferred to Phase 5 (recorded now so it is not forgotten; out of Phase-1/2 scope):**
- **Phone format validation** — libphonenumber-style check on a sampled phone column (area + 7-digit, no split fax lines). Prevents a quiet TX-mechanical sequel where a "filled" field is not a *valid* number.
- **Role-email down-weighting** — see §9; the hard-drop version is intentionally rejected.

---

## 7. Status model (replace the dead flag)

```
              ┌─────────────────────────────────────────────┐
 untried ─▶ probing ─▶ adapter_draft ─▶ probation ─▶ promoted
             │              ▲               │     │        │
             ▼              └─ schema_      │     │   exhausted(30d re-arm)
        blocked (403/429) ── mismatch───────┘     │   blocked_all_paths(30d re-arm)
        │  → retry next access path               └─ dead (404/410 only) → skip
        └──────────────────────────────────────────────┘
```

- **blocked** ⇢ next access path (auto), not permanent.
- **dead** ⇢ 404/410 only — permanent.
- **exhausted** ⇢ dup-rate > 90% across 3 consecutive fetches, 30-day re-arm (boards update monthly).
- **schema_mismatch** ⇢ capability downgrade + adapter re-write (drift): on gate failure or a fetch error after promotion, the source returns to `probing` — it is never silently retired; a fresh adapter is written and re-validated.
- **per-state staleness watch** ⇢ a state stuck in `adapter_draft`/`blocked` for > `STALE_STATE_DAYS` (default 7) without promoting sets `manual_review` = true (dashboard flag). Computed from `status` + `last_verified_at` — no new schema. The aggregate 3×0 alarm only catches a whole-pipeline stall; one hard case (WAF-heavy, unusual encoding) hides behind other states' liveness, so the watch is what surfaces it.

---

## 8. Router — demand × capability (the two verticals)

The Demand table becomes the single intake; the Router is the only consumer.

**Demand fan-out (option a — chosen):** state-level rows (`trade=''`) cross-join with the canonical trade taxonomy, weighting the canonical trades by observed share → real fulfillable `trade × state` pairs. AK × [gc, electrician, plumbing, roofing, …]. This fixes the 50 structurally-dead rows.

**Routing rule (both lanes):**
- **phones lane** ⇢ demand pair binds only to `promoted` sources where `capabilities.phone = true`.
- **emails lane** ⇢ demand pair binds only to `promoted` sources where `capabilities.email = true` (bulk) — **plus** the existing web-discovery path (`run_full`: search → crawl → decision-maker extraction) for uncovered pairs.

**Quota ceiling (derived, not fantasy):**
```
today_ceiling[vertical] = Σ_over_promoted_sources  (remaining_rows × capabilities[c].fill_rate × dedup_survival_rate)
```
The dashboard then reports "N% of *achievable*", killing the fake 20% / 6.8% panic that drove wrong optimization.

---

## 9. Emails — the honest split (the critical clarification)

This architecture is packed around **source adapters**, and source adapters produce what a board/dataset publishes. **State licensing boards mostly publish phones, addresses, and license numbers — not emails.** So the phones vertical is fixed *completely* by the seed list; emails are fixed in three different ways:

| Emails concern | Fix | Where |
|---|---|---|
| **Quantity — bulk seed** | `email_sources.csv`: Overture (74M CDLA, has email+phone), Common Crawl snapshot, email-bearing boards, public directories — same adapter pipeline, `capabilities.email=true` | this architecture (§2.2, §8) |
| **Quantity — more pairs** | demand fan-out (a) → emails lane no longer starved at 3 pairs | this architecture (§8) |
| **Quality — the 10:1 funnel** | 3-tier pre-filter **before storage**: syntax+domain-exists → MX/catch-all/dead-domain → **role filter as DOWN-WEIGHT, not hard-drop** — small/mid contractor shops often route the owner through `info@`, so `info@`/`sales@`/`contact@` get a low decision-maker weight combined with company-size signal, not deletion. Bottom-50% dork templates auto-retire. Budget freed from wasted phone trials is reinvested here. | this architecture + V1 email modules |

**Bottom line:** the seed-list pipeline alone lifts **phones**, and lifts **email quality + modest email quantity**; email **quantity at scale** additionally requires the `email_sources.csv` tier, which the same pipeline consumes for free. Build both seeds, run one pipeline.

---

## 10. Exhaustion, refresh, and rot

- **API pagination** — persist `cursor`/`last_seen_id`.
- **Bulk files** — `fetch.if_modified` (ETag/Last-Modified) + row-level `record_hash` dedupe (no pagination exists on a file; hash is the only honest cursor).
- **Exhaustion** — 3 consecutive fetches with dup > 90% → `exhausted`, drop from scheduler, 30-day re-arm. This is what the phones collapse (2,346 → 638 → 6) should have triggered.
- **Format rot** — any fetch error or gate drift after promotion → back to `probing`, adapter rewritten. Agency layout changes are normal, silent retirement is not.

---

## 11. Telemetry (one north-star)

```
usable_rate[source] = verified_working_records / total_fetched_records   ← start tracking total_fetched
```

Per-state, per-trade, per-source coverage map + `usable_rate` drive every decision: promote/reject/retire, which dork template gets budget, where the router points demand. Without `total_fetched`, no quality claim is measurable.

**Outcome loop (closes supply → revenue, Day-1 parallel):** CRM grows 4 columns — `contacted_at, replied_at, meeting_booked, disqualified_reason`. Two weeks after 46 states promote, the question "kaunsa state/trade/source actually paisa laa raha hai" becomes answerable by query, not by feel.

---

## 12. Build order (phase-gated — one per approval)

| Phase | Deliverables | Exit criteria (tests) |
|---|---|---|
| 1 | `state_license_boards.csv` + `email_sources.csv` + `boards.py`; status-model split (blocked/dead/schema/exhausted); source_registry migration (access_path, capabilities, trade_mapping, estimated_rows, rows_consumed, jurisdiction split) | seed-list invariants; transition tests; router never binds phone demand to `capabilities.phone=false` |
| 2 | Access Prober (5 paths, 403→next-path) + Validator (gate 0 + 5 gates + re-measure) — pure Python | probe fake-HTTP matrix: 403/404/200 each → correct status + reason; gate unit tests |
| 3 | Adapter Writer (AI contract) + probation runner — **CA CSLB pilot only** | `ca_cslb_bulk` promoted via full pipeline on test server; 500-row dry run |
| 4 | Scout cron switched to V2, old proposal stage deleted | scout_pass V2 output; zero `"AI selected none"` no-op runs |
| 5 | Queue all 46 states **by `priority_rank`** + email seeds (named, real rows); telemetry (usable_rate, coverage map); outcome-track cols; per-state staleness watch (§7); phone-format validator + role-email down-weight (deferred §6/§9) land here | fresh-state promotion **floor ≥ 1/week = liveness alarm only, not the pace** (real pace ~1-2 states/day via hourly cron + 1-day probation → full US in weeks, not the 11 months that a literal floor reading implies); emails quantity from Overture/CommonCrawl; ceiling dashboard |

**Schedule honesty:** Phases 1–2 are deterministic and ride the 5-day plan comfortably. Phase 3's AI adapter-writer is where schedule slips (bulk file diversity), so it is capped at a 1-state pilot before any flood.

---

## 13. What this does NOT do (risks, stated)

- Increases **supply**, not **demand** — without the outcome-tracking loop, more coverage does not equal more revenue (mitigated by §11).
- Does not invent emails where boards publish none — email *quantity* is capped at what `email_sources.csv` + web discovery yield.
- Same company across two sources is one dossier **only** via the ER feed (§3) — skip it and outreach double-hits.
- A `source_registry.json` seed row is a *hint*, not a license — every promotion still requires real fetch + validator + probation evidence.