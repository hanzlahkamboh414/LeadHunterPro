# LeadHunter Pro — Complete Roadmap

> Live project map: where we are, how far it's built, and what comes next.
> Maintained against the verified test evidence (each increment gated on approval).
> Last updated: 2026-08-10 · Branch: master @ 63fa994 (Sprint2.4) + working tree

---

## 1. Vision & Permanent Goal

LeadHunter Pro is a **REAL, production-grade company-discovery platform**, not a
search-API client. Target domain: construction/contractor leads, **Texas-first**.
The business behind it (The Best Estimator LLC) sells construction estimation /
preconstruction services, so a *qualified lead* is:

> A company that is **provably in a buying window** AND has a **reachable
> decision-maker** who owns estimation/bidding decisions — never a company with
> a generic mailbox.

**V1 target:** 15–20 qualified Texas contractor leads, end-to-end, **zero budget /
free-keyless sources only**.

The "Execute" button must eventually perform REAL discovery (CLAUDE.md §10):

```
Search Internet
    ↓  Find Companies
    ↓  Visit Websites
    ↓  Extract Company Information
    ↓  Extract Decision Makers
    ↓  Validate
    ↓  Rank
    ↓  Return Results
```

It must NEVER become: `Read Fixture JSON → Return Cached Companies`.
Fixtures exist ONLY as an emergency bridge — surfaced honestly as
`data_source="fixture"`, `bridge_mode=true`, with a `fallback_reason`.

---

## 2. Architecture North Star

Correct flow (CLAUDE.md §2, §4 — every source replaceable):

```
Discovery Engine
    ↓
Source Orchestrator            (SourceOrchestrator)
    ↓
Multiple Sources               (DirectoryCrawlSource · SearchProviderSource · FixtureSource · intent plugins)
    ↓
Crawler / Extractor            (HttpCrawler, session manager, website extractors)
    ↓
Validation + Ranking           (Phase 2 AcceptanceGate + CompanyScorer)
    ↓
Results
```

**Forbidden:** `Connector → Brave API → Results`. Search APIs are optional,
replaceable infrastructure — never the core. Business logic never depends on a
single provider (§4).

Layer direction: `api → services → engines → connectors → crawlers`.
Dependencies flow down only.

---

## 3. V1 Qualification Gate — Definition of a Qualified Lead

Encoded in [lead_models.py](backend/app/engines/lead/lead_models.py) as a
deterministic gate (`Lead.qualification_gate`). A lead clears **ONLY** when every
hard rule holds; the gate reports EVERY missing requirement (`blocked_by`):

| # | Hard rule |
|---|---|
| 1 | **Verified company** — passed `AcceptanceGate` (`metadata["gate_accepted"]`) |
| 2 | **Named decision-maker** with `role_relevance=True` (owner, PM, estimator, procurement, operations, …) |
| 3 | **HARD** — a `person_bound` email (e.g. `f.last@company.com`). `info@`/`contact@` NEVER qualify |
| 4 | Phone is **OPTIONAL** — never blocks |
| 5 | **≥1 buying-intent evidence with a traceable `source_url`** — never invented evidence |
| 6 | `ai_confidence ≥ 90` backed by real evidence **+ a written justification citing that evidence** |

Tiers (honest V1 scope):
- **Email:** `format` < `domain` (MX resolves) < `person_bound` — only `person_bound` qualifies.
- **Phone:** `format` only (deliverability deferred).
- **Person identity:** `unverified` (site-derived best-effort); `role_relevance` gates separately.

---

## 4. What Is Built — Layer by Layer

### ✅ Foundation layers (Sprint 2.x migration, all verified)

| Layer | Location | Status |
|---|---|---|
| Crawlers | `backend/app/crawlers/` — http_crawler, session_manager, html_parser, response, retry, cache, rate_limiter, robots | ✅ built + tested |
| Connectors | `backend/app/connectors/` + `backend/app/engines/source_connectors/` — TexasProcurement, AGC Texas, connector_manager, deduplicator, error_handler, http_client, normalizer, retry, evidence | ✅ built + tested |
| Search providers (replaceable infra) | `backend/app/search_providers/` — Brave, SearXNG, manager, registry | ✅ built + tested |
| Discovery sources | `backend/app/discovery/sources/` — SourceOrchestrator, SearchProviderSource, DirectoryCrawlSource, FixtureSource, honest `SourceStatus` (EMPTY ≠ UNAVAILABLE) | ✅ built + tested |
| Website discovery machinery | `backend/app/discovery/website/` — PageFetcher, HTMLParser, URL normalizer/filter, confidence, extractors (email/phone/leadership/services/address/…), generators (brave/searx/fixture) | ✅ built + tested |
| Plugin framework | `backend/app/discovery/plugins/` — `BaseDiscoveryPlugin`, `PluginCapability` (incl. PROJECT/NEWS/BID/PERMIT_DISCOVERY), `PluginRegistry`, `PluginManager`, `PluginSource` | ✅ built + tested |
| Source intelligence | `backend/app/engines/source_intelligence/` — SourcePlanner, source_models | ✅ built + tested |
| Verification (Phase 2) | `backend/app/engines/verification/` — **AcceptanceGate**, identity/industry/location verifiers, source_tiers | ✅ built + tested |
| Company discovery engine | `backend/app/engines/discovery/company/` — engine, cleaner, validator, models | ✅ built + tested |
| AI | `backend/app/ai/` — gateway, providers (local/openai/gemini), prompts, **CompanyScorer** (`scorer.py`) | ✅ built + tested |
| API surfaces | `backend/app/api/v1/` — discovery, connectors, leadership, email, research, source_intelligence, health | ✅ built |
| Demo orchestrator | `backend/demo.py` — thin runner over the EXISTING pipeline; JSON/Excel export; honest bridge reporting | ✅ built |

### ✅ Lead-pipeline increments (this plan, gated)

| Inc | Deliverable | Status |
|---|---|---|
| 0 | Lead schema contract + deterministic gate (`engines/lead/`) | ✅ DONE & verified |
| 1 | Email format layer wired into discovery path | ✅ DONE & approved |
| 2 | Decision-maker extraction + person↔email binding | ✅ DONE & verified |
| 3 | Phone endpoint + normalizer (format-only) | ⏸ DEPRIORITIZED — never blocks |
| 4 | Email **domain/MX** tier (`domain_verifier.py`) | ✅ DONE & verified |
| 5 | **Buying-intent plugins** (company_site · google_news · usaspending) | ✅ DONE & verified |
| 6 | Extend CompanyScorer → `ai_confidence ≥90` + evidence-citing justification | ⏳ NEXT |
| 7 | LeadPipeline orchestration (reuse demo.py exporter) | ⏳ pending |
| 8 | Live coverage run → 15–20 qualified leads | ⏳ pending |

---

## 5. Increment Detail — Built So Far

### ✅ Inc 0 — Lead schema contract
`backend/app/engines/lead/lead_models.py` (+ `lead_samples.py` fixtures):
`Lead`, `LeadPerson`, `LeadEmail`, `LeadPhone`, `IntentEvidence`,
`EmailVerificationTier`, `IntentEvidenceType`, `LeadAI`, and the deterministic
`qualification_gate` encoding all 6 hard rules. `qualifies` is **derived** from
the gate — can never drift.
→ **40 offline tests, ruff clean.**

### ✅ Inc 1 — Email format layer wiring
`EmailDiscovery` / `WebsiteEngine.parse` / `demo.py --enrich` reuse the existing
`app/email/` format helpers (`EMAIL_CLEAN_PATTERN`, `is_valid_email`,
`clean_emails`). No new email system.
→ **8 new offline tests, ruff 0 new.** (9 pre-existing findings on those files
left per founder choice — out of scope.)

### ✅ Inc 2 — Decision-maker extraction + person↔email binding
`backend/app/discovery/people_parser.py` rewritten — **role-anchored names** via
backward/forward token scan (old greedy regex that grabbed the company name is
gone), noise + company-name guard, region email→person binding with honest tiers
(personal local-part → `person_bound`; generic → `format` via
`is_generic_email_local_part`). Emits `PersonRecord(person=LeadPerson(
role_relevance=…, tier=unverified), emails, region_text)`. Caller
`leadership_discovery.py` emits the new output shape.
→ **18 offline tests (real-HTML fixtures), ruff 0 new on authored files.**

### ⏸ Inc 3 — Phone endpoint (format-only)
Deprioritized by founder — optional, never blocks. `LeadPhone` already carries
the `format` tier; a future increment adds the endpoint + normalizer.

### ✅ Inc 4 — Email domain/MX tier
`backend/app/email/domain_verifier.py` (NEW):
- `domain_has_mx(domain)` — **zero-key** DNS-over-HTTPS MX check via
  `dns.google/resolve`. Non-200 / malformed / network error → False (honest
  "unknown", never a false positive).
- `verify_email_domains(emails)` — upgrades `format`→`domain` per unique domain;
  `person_bound` never re-verified or downgraded.
- Wired into `LeadershipDiscovery.discover()` — the only place `LeadEmail`
  records are produced.

**MX-record required by design:** A-only domains stay `format`. Domain tier is an
additive signal, NOT a V1 qualification gate.
→ **18 offline tests (monkeypatched requests), ruff 0 new.**

### ✅ Inc 5 — Buying-intent plugins  *(just delivered)*
New `backend/app/discovery/intent/` package — capability-isolated, free/keyless,
traceable evidence:

| Plugin | Capability | Source | Evidence type |
|---|---|---|---|
| `CompanySiteIntentPlugin` | `PROJECT_DISCOVERY` | crawls company's own site for conservative active-buying phrases (now hiring / currently working on / expanding to) | `project` / `hiring` / `expansion` |
| `GoogleNewsPlugin` | `NEWS_DISCOVERY` | Google News **RSS** search on company name (+ location) | `news` |
| `USAspendingPlugin` | `BID_DISCOVERY` | public USAspending award-search API, keyless | `bid_award` |

Design principles (hard rules #1, #5):
- **Honest outcomes** — matched signal → traceable `IntentEvidence` (source_url =
  exact page); quiet site / empty feed → `EMPTY`; unreachable → `UNAVAILABLE`; the
  two never collapse.
- **An untraceable signal is NOT evidence** — an award with no Award ID is dropped.
- **Capability isolation** — intent plugins declare PROJECT/NEWS/BID_DISCOVERY
  only, never COMPANY_DISCOVERY, so they can't leak into company discovery
  (uses the existing `attach_plugin_source` seam).
- **Reuse** — rides the existing plugin framework + `PageFetcher`/`HTMLParser`
  machinery; no new abstractions, no new dependency, no network in tests.
- Registration: `register_intent_plugins(registry)` — idempotent.
→ **26 offline tests (saved real-HTML + monkeypatched requests), ruff 0 new.**

**Full offline suite is green** — 1510 passed / 29 deselected at the Inc-0 gate,
plus 70 focused tests added across Incs 1/2/4/5.

---

## 6. What's Next — Increment Detail

### ⏳ Inc 6 — Extend the ONE CompanyScorer (intent-backed confidence + justification)
Current gap: `CompanyScorer.qualify()` produces `score / qualified /
qualification / reasons / strengths / concerns` — but the Lead gate needs
`ai_confidence` (0–100, threshold **90**) and a **justification that cites the
specific intent evidence** (source_urls). Rules:
- Extend the single `CompanyScorer` — **no second AI system**.
- Feed it the collected `IntentEvidence`; require the prompt to evaluate the
  supplied evidence ONLY and never invent facts (prompt already encodes this).
- Deterministic fallback on AI failure (never blocks, honest).
- Offline tests with a stub provider. Expected: `ai_confidence ≥ 90` only when
  real, traceable evidence backs it.

### ⏳ Inc 7 — LeadPipeline orchestration
Assemble a `Lead` end-to-end from existing pieces (reuse, no new discovery
engine):
```
CompanyDiscoveryResult (gate_accepted ✓)
    → LeadershipDiscovery → person + emails
    → verify_email_domains()           (Inc 4)
    → intent plugins by capability     (Inc 5: PROJECT/NEWS/BID)
    → CompanyScorer.qualify()          (Inc 6: ai_confidence + justification)
    → Lead.qualification_gate()        (Inc 0 — deterministic verdict)
    → export via the demo.py exporter  (JSON/Excel)
```
Wire into an API surface; offline tests over the full assembly.

### ⏳ Inc 8 — Live coverage run
Run the real pipeline over live (keyless) sources, collect the qualified leads,
produce the verification report. **Success = 15–20 qualified Texas contractor
leads**, each with traceable evidence (source_urls) and a decision-maker reachable
via `person_bound` email.

---

## 7. Standing Workflow Rules (how we build — never violated)

1. **One increment at a time** — never start the next automatically; STOP after
   each phase and wait for explicit approval.
2. **Sequential manual verification** — ONE command at a time, wait for pasted
   output; never send Step N+1 before N passes. Checklist never restarts.
3. **Test invocation:** always `python -m pytest` **from `backend/`** (bare
   `pytest` fails; no ruff/pytest config is checked in).
4. **Ruff gate:** `Found 0 errors` on authored files (pre-existing findings on
   legacy files stay out of scope per founder choice).
5. **Accuracy-first:** fixtures never primary; live-first; honest `EMPTY` vs
   `UNAVAILABLE`; never fabricate evidence; a blocked verification never becomes
   a "PASS".
6. **No implementation while verification is pending.**
7. **Never retry a blocked Bash/PowerShell command** — queue the verification
   card instead; attempt once only.
8. **Never commit unless asked** (CLAUDE.md §13 — no reset/restore/checkout/
   revert/clean/force-push without explicit approval).
9. **Reuse before build** (§14) — search for existing implementations first.
10. **Zero budget / free-keyless only** — no paid API keys.

---

## 8. Deferred / Known Limits (explicitly out of V1)

- **Permits** (`PERMIT_DISCOVERY`) — deferred because the clean sources are
  paid. Stays an open `IntentEvidenceType.permit` for a future paid tier.
- **Phone** — format-only in V1; deliverability / ownership later.
- **Email deliverability** — V1 stops at `domain` (MX); inbox-level checks later.
- **Decision-maker identity verification** — `unverified` by design.
- **Search providers** — optional/replaceable infrastructure, never core.
- **Git commit roadmap** — commit work is **ON HOLD**; migration work proceeds as
  uncommitted, gated implementation.

---

## 9. One-Line Status

**Increments 0–5 built & verified (26 intent-plugin tests green, ruff clean);
Increments 6–8 remain — next is Inc 6 (extend CompanyScorer for intent-backed
confidence ≥90 + justification), then Inc 7 orchestration, then Inc 8 live run to
the 15–20 qualified-lead target.**
