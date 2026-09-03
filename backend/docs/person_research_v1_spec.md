# Person Attribution Research — V1 Architecture Specification (REVISED)

**Status:** LOCKED — corrections applied per audit; ready for implementation green light.
**Date:** 2026-09-03 (rev.2 — audit corrections incorporated)
**Scope:** Person attribution for unattributed company emails (`person=None` + ≥1 non-generic email). Website + Indexed Web only. Phase A/B/C/D1, gate, and pipeline are **frozen and untouched**.

This revision incorporates the 14 audit corrections. The audit itself is not reproduced here; only the corrected spec.

---

## 1. Integration Boundary

```
Parser (A/B/C/D1) ──► Plan-holder record (person=None, emails=[...])
                              │
                              │  (per-eligible-email: non-generic, non-free-mail,
                              │   not already person_bound)
                              ▼
                 PERSON RESEARCH SIDE-BAND (additive, NEW)
   ┌──────────────────────────────────────────────────────────────┐
   │  store → triage → website → indexed-web → candidates          │
   │  → boolean rules → verdict → append-only evidence → persist   │
   └──────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
                 ResearchApplier (controlled adapter — NEW, in package)
                              │  ONLY writes person + person_bound
                              │  when verdict == attributed
                              ▼
                      Lead ──► qualification_gate()  (UNCHANGED)
```

**Completely untouched (byte-identical):** `pdf_plan_holder_parser.py` (A/B/C/D1), `acceptance_gate.py`, `qualification_gate()` / `lead_models.py`, `lead_pipeline.py`, `leadership_discovery.py`, `people_parser.py`, `run_leads.py`, and all existing Phase A/B/C/D1 tests.

**Additive (new):** `app/person_research/` package + a runner script + SQLite store.

**Integration rule (audit corr. 12/14):** research **reads** the lead-record dict and produces a `ResearchResult`. It **never** mutates the Lead directly. A **`ResearchApplier`** adapter — living in the new package — reads the verdict and, **only when `verdict == attributed`**, writes `plan_holder.person` + `person_bound` onto the lead-record dict. All new models live in the package, **not** `lead_models.py`. No frozen file is modified.

**Reused read-only:** `PeopleParser.extract_candidates`, `role_relevance`, `is_free_mail_domain` — via adapters (see §14). `LeadershipDiscovery` is **NOT** used in V1 (deferred; audit corr. 14).

---

## 2. Research Status vs Attribution Verdict — SEPARATE

Two orthogonal enums. Research Status is operational (drives resume/retry); Attribution Verdict is semantic (drives gate/UI/reports).

### ResearchStatus (execution / machine-facing)
`pending` → `researching` → `completed` | `failed`

### AttributionVerdict (conclusion / product-facing)
`unattributed` | `candidates_found` | `attributed` | `unresolved`

- `unattributed` — no eligible email, or researched and nothing usable found.
- `candidates_found` — ≥1 candidate, none confidently bound (may resolve with a new source).
- `attributed` — exactly one person bound.
- `unresolved` — **decision-to-not-decide**: contradictory or genuinely indeterminate (audit corr. 1). Contradiction ⇒ `unresolved`, always.

### Legal (status, verdict) combinations

| ResearchStatus | Verdicts allowed |
|---|---|
| pending | unattributed |
| researching | unattributed / candidates_found |
| completed | unattributed / candidates_found / attributed / **unresolved** |
| failed | unattributed / candidates_found |

- Contradictory evidence → verdict `unresolved` (ResearchStatus may be `completed`).
- Failed research attempt → ResearchStatus `failed` (verdict stays `unattributed`/`candidates_found` if partial evidence preserved).
- Research **can** be `completed` while attribution is `unresolved` — this is legal and common.

---

## 3. Research Queue / Store — SQLite (audit corr. 3)

**MVP store = SQLite** (one file) for BOTH jobs and all caches. Atomic transactions, durable, concurrent-safe. **No Redis/message broker.** No lease/claim machinery in V1 (single-process): resumability comes from a `jobs` table with status + `sources_checked`.

**Job record (`jobs` table, keyed by `email_hash`):**

```
email_hash, email, domain_hash,
research_status, verdict,
sources_checked, source_errors,
attempts, last_researched_at, research_version
```

**Lifecycle:**
- **Create:** idempotent insert on `email_hash`. Never re-create. One job per distinct eligible email (audit corr. 2).
- **Claim/process:** pick `pending` jobs, set `researching`, run stages, persist after every stage, set terminal status.
- **Resume after crash:** on start, any `researching` job is returned to `pending` (its `sources_checked` skips completed stages). SQLite WAL gives crash-safe persistence.
- **Retries:** per-source exponential backoff, ≤ `MAX_SOURCE_RETRIES`. A failing source → recorded in `source_errors`, skipped on resume; other sources still run.
- **Source-level failure:** never fails the job alone. Job `failed` only when **all** applicable sources failed AND no usable result (audit corr. 11).
- **Idempotency:** all writes keyed by `email_hash`; re-runs are cache hits, no duplicate work.
- **Duplicate prevention:** unique `email_hash`; shared `domain_hash` crawl cache; no two jobs for one email.
- **Manual requeue:** set `research_status=pending`; **keep** candidates + evidence (append-only); bump `research_version` only to force re-research.
- **Scheduled re-research:** out of V1. No cron.

---

## 4. Research Unit — PER ELIGIBLE EMAIL (audit corr. 2)

- **Research unit = one job per distinct eligible email** (`email_hash`). Not per-lead, not per-lead-primary-email.
- **Eligible:** non-generic local-part, non-free-mail domain, **not already `person_bound`**.
- **Already `person_bound` email → skipped** — research never overwrites an existing binding.
- **Attribution outcome = per person/email pairing.** Each email builds its own candidates + verdict strictly from its own local-part + its own co-occurrence context.
- A lead with 2–5 eligible emails → 2–5 jobs, all sharing one domain crawl (cache) but each attributed independently.
- **Cross-email rule:** never bind Email A to the person evidenced for Email B. Never attribute Person A's email to Person B because both share a company/domain.

---

## 5. V1 Waterfall (deterministic)

### Stage 0 — Triage
- **Input:** lead-record dict (person=None, emails[]).
- **Operation:** for each email, compute eligibility (non-generic, non-free-mail, not already person_bound). Emit one eligible email → one job; skip the rest.
- **Output:** per-email normalized address + email_hash.
- **Exit:** no eligible email → verdict `unattributed`, status `completed`.
- **Escalation:** eligible → Stage 1 (per email).
- **Failure:** none (local).

### Stage 1 — Company Website
- **Input:** normalized email (+ domain).
- **Source:** own scoped crawler + `PeopleParser.extract_candidates` (adapter).
- **Operation:** discover site (email domain, else plan-holder website). Fetch only people-identification pages (priority: homepage → contact → about → team → staff → people → leadership → directory), ≤ `MAX_WEBSITE_PAGES` (6), single domain, robots-respecting. Extract candidate names/roles + all emails seen + their **co-occurrence contexts** (which name is in the same contact-block/region/mailto as which email).
- **Output:** domain-level facts → per-email candidate evidence.
- **Cache:** full crawl result under `domain_hash` (shared by all same-domain emails). Facts only — no attribution implied. Page content not stored (snippets only).
- **Exit:** verdict reached (attributed or contradiction→unresolved) → stop.
- **Escalation:** signal present, not yet confident → Stage 2. No signal / site unreachable → Stage 2.
- **Failure:** site down/blocked → record website `source_error`, go to Stage 2.

### Stage 2 — Indexed Web
- **Input:** normalized email + candidate shortlist (if any).
- **Source:** search API (Tavily seam). Queries, deduped, ≤ `MAX_INDEXED_SEARCHES` (4):
  1. exact `"<local-part>@<domain>"`
  2. `site:<domain> <local-part>`
  3. `<domain> team|staff|about <lastname>` — **only when** a Stage-1 candidate needs role/identity corroboration.
  Fetch only promising URLs (≤ `MAX_INDEXED_FETCHES` 3).
- **Output:** corroboration/contradiction evidence for candidates.
- **Cache:** per-query under `(email_hash, query)`; TTL.
- **Exit:** verdict reached → stop. Else exhausted → `candidates_found` / `unattributed`.
- **Escalation:** none in V1 (Stage 3/4 deferred; §18).
- **Failure:** search API down → record `source_error`; if website already gave a confident binding → `completed`/`attributed`; if partial candidates → `completed`/`candidates_found`; `failed` only if all sources failed AND no usable result (audit corr. 11).

---

## 6. Candidate Model

```
PersonCandidate:
  name: str                      # REQUIRED
  role: str = ""                 # OPTIONAL — only when found (never invented)
  role_relevance: bool = False   # OPTIONAL — via existing helper when role present
  confidence: float = 0.0        # REQUIRED — 0-100, RANKING ONLY (audit corr. 7)
  evidence: list[ResearchEvidence]  # REQUIRED — ≥1 traceable evidence
  local_part_match_level: str    # REQUIRED — exact|first_last|initial_last|first|reject (§7)
  co_occurrence: bool            # REQUIRED — tight same-block co-occurrence
  corroboration_count: int       # REQUIRED — # INDEPENDENT sources (§8)
  authority_evidence: bool       # REQUIRED — ≥1 authoritative (company-site) evidence
  bound: bool = False            # REQUIRED — true only when verdict=attributed
  contradictory: bool = False    # REQUIRED — any direct contradiction
```

**Required** (populated before any verdict): all above except `role`/`role_relevance` (optional). Candidate dedupe key: `(normalized name, role, email)`.

---

## 7. Evidence Model + Local-Part Matching

### Evidence
```
ResearchEvidence:
  source_url: str           # REQUIRED — exact URL
  source_type: str          # REQUIRED — company_site | indexed_web | directory
  authority: str            # REQUIRED — authoritative | supporting
  snippet: str = ""         # OPTIONAL — ≤300 chars, NOT full page
  retrieved_at: str         # REQUIRED — ISO timestamp
  evidence_kind: str        # REQUIRED — email_present | local_part_match | name_co_occurrence | role | corroboration | contradiction
  canonical_url: str        # REQUIRED — deduped canonical form (§16)
```

**Storage:** snippets only; page body discarded; **append-only per email_hash** (audit corr. 13). Dedupe by `(evidence_kind, canonical_url, snippet_hash)`.

### Local-part matching — leveled, supporting-only (audit corr. 6)

| Level | Example | Contribution |
|---|---|---|
| exact | `jane.smith@` ↔ "Jane Smith" | strong |
| first_last | `jane.smith` ↔ Jane Smith | strong |
| initial_last | `jsmith` ↔ John Smith | medium (capped) |
| first only | `jane` ↔ Jane | weak — supporting only |
| initials | `js` | **reject for binding** |
| nickname | `bob` ↔ Robert | ambiguous — reject unless corroborated |
| partial/typo | `jan.smith` ↔ Jane | **reject for binding** |

**Rule:** local-part match is a **supporting signal only** — it contributes to ranking but never attributes by itself. Initials/nickname/partial never bind.

---

## 8. Confidence / Scoring — BOOLEAN GATE, SCORE RANKING-ONLY (audit corr. 7)

### Step A — Hard blockers (any true ⇒ cannot bind, score irrelevant)
1. Generic local-part (`info@`, `contact@`, `sales@`, `jobs@`, `support@`, `no-reply@`, `hr@`, `admin@`, `hello@`) → excluded.
2. Free-mail domain → excluded.
3. **No tight co-occurrence** (email never appears in the same contact-block/region/mailto as a name) → cannot bind.
4. **Local-part does not match the co-located name** (not merely "a name is near the email") → cannot bind.
5. **Multiple names near one email** (ambiguous) → cannot bind.
6. **Any contradiction** → **UNRESOLVED**, immediately.
7. Stale evidence (old-employee / expired cache) → weak, cannot bind.
8. Alias/forwarding address → cannot attribute.

### Step B — Deterministic score (0–100, RANKING ONLY)
Additive, capped; used to rank candidates, **never** to decide binding:

| Signal | Weight |
|---|---|
| Local-part ↔ co-located name match (exact/first_last) | 30 |
| Tight co-occurrence (same block) | 25 |
| Authoritative company-site source | 20 |
| Independent corroboration (per extra origin, cap 3) | 5–15 |
| Role found + role_relevance | +5 |

Penalties: ambiguity (many names near email) −15; no authoritative source → cap 50; partial local-part → cap 10.

### Step C — Binding decision (BOOLEAN — all must hold)
**`attributed` iff:** no hard blocker ∧ tight co-occurrence ∧ local-part matches co-located name ∧ (independent corroboration ≥ 1 ∨ authoritative company-site pairing) ∧ no contradiction.

**Score does NOT override any blocker or contradiction.** A high score never binds; score only orders candidates when multiple exist.

---

## 9. Independent Source Definition (audit corr. 8)

**Independent = distinct canonical origin** (different host AND different authoring entity), and neither is a mirror/republication of the other.

| Pair | Independent? |
|---|---|
| company site + separately-maintained directory | ✅ |
| company site + Google cached snippet of SAME page | ❌ |
| same PDF mirrored on 3 sites | ❌ (one source) |
| two pages on same company website | ❌ (one host/author) |
| two search results → same canonical URL | ❌ (dedupe) |

Corroboration counts only across independent origins.

---

## 10. Multiple Candidates — Decision Algorithm

Evaluate all through Step A/C; bind only when exactly one satisfies the boolean rule and it dominates:

| Scenario | Decision |
|---|---|
| Exactly 1 passes boolean rule | **Bind** |
| 1 passes but lacks corroboration+authority | candidates_found |
| 2 candidates | bind only if one passes rule AND the other clearly fails; else unresolved |
| several candidates | unresolved unless exactly one dominates on both rule *and* local-part |
| 1 strong local-part match, others weak | bind only if it ALSO has tight co-occurrence + corroboration; local-part alone insufficient |
| 2 similar confidence | unresolved — never choose near-ties |
| strong site evidence, no corroboration | candidates_found (single source not binding) |
| corroboration but no co-occurrence | candidates_found (person corroborated ≠ email belongs to them) |

**Never auto-bind ambiguity.**

---

## 11. Attribution Safety Rules (HARD — become tests)

1. No username inference.
2. No domain inference.
3. No guessing — ambiguity ⇒ blank/unresolved.
4. No fabricated evidence — every signal traceable to source_url + snippet.
5. No invented emails.
6. No invented roles.
7. No social profile as identity authority (deferred from V1 entirely).
8. No binding without the full boolean rule set.
9. Contradiction ⇒ unresolved, never overridden by score.
10. Preserve unresolved leads — never discard; requeue-able.
11. Tight co-occurrence required for binding.
12. Precision > recall — a wrong attribution is the worst outcome.

---

## 12. Cache / Versioning (audit corr. 10)

- **Primary identity:** `email_hash = sha256(normalized_email)`. Normalize: trim; lowercase domain; strip `mailto:`; drop `+tags`/dots only after confirming free-mail (then unresearchable anyway); IDNA-encode Unicode domains; reject invalid at triage. Always hash the normalized form.
- **domain_hash:** shared Stage-1 crawl facts (page inventory, name/role pool, email→co-occurrence-context pairs). TTL 30 days; never implies attribution.
- **query_hash:** per-query indexed-web result. TTL 30 days.
- **Verdict cache:** **never auto-expires** — a bound person must not silently unbind; invalidate only via manual requeue / version bump.
- **Versioning split (audit corr. 10):**
  - `research_version` — bump when **sources or evidence schema** change → triggers re-research.
  - **Re-scoring** (weights/threshold change) — re-run scoring over **stored evidence**; **NO re-research, NO external calls.**
- **Invalidation:** manual requeue / version bump only. No cron.

---

## 13. Duplicate / Batch Optimization

- One domain crawl shared by all same-domain emails (`domain_hash` cache); email attribution stays per-email.
- Same email in multiple leads → one `email_hash` job, shared verdict, multiple refs.
- Same person across pages → dedupe candidate by (name, role, email).
- Duplicate/mirrored search results → dedupe by canonical URL / page hash.
- Duplicate evidence → dedupe by (evidence_kind, canonical_url, snippet_hash).
- 50 same-company leads → 1 website crawl + handful of deduped queries, not 50×6.

---

## 14. Existing Component Reuse (adapters; no modification)

| Existing | Reused as | Via |
|---|---|---|
| `PeopleParser.extract_candidates` | Stage-1 name/role/email extraction + regional email binding | Adapter (feed page HTML; add tight co-occurrence check) |
| `role_relevance` helper | `PersonCandidate.role_relevance` | Direct reuse |
| `is_free_mail_domain` | triage / free-mail exclusion | Direct reuse |
| `LeadershipDiscovery` | **NOT used in V1** (deferred) | — |

Adapters live in `person_research/sources.py`. Components are not modified.

---

## 15. Failure & Recovery (deterministic — audit corr. 11)

| Event | Behavior |
|---|---|
| Website unavailable/blocked | website `source_error`; go to Stage 2; honor robots.txt |
| Search API unavailable | search `source_error`; if website gave confident binding → completed/attributed; if partial → completed/candidates_found; `failed` only if all sources failed AND no usable result |
| Timeout | per-request timeout; mark fetch failed; continue |
| Malformed response | source failure; log; continue |
| No candidates | completed / unattributed; lead preserved |
| Contradictory candidates | **unresolved**, never bind; lead preserved |
| Temporary network failure | backoff ≤ MAX_SOURCE_RETRIES, then source_error |
| Process crash | `researching` → `pending` on restart; `sources_checked` skips done stages; SQLite WAL durable |
| Partial research | evidence append-only per stage boundary; resume from next un-run stage |

**Invariant:** no lead disappears on any failure; status always legal; queue always resumable.

---

## 16. Execution Budget

| Limit | Default |
|---|---|
| MAX_WEBSITE_PAGES | 6 |
| MAX_INDEXED_SEARCHES | 4 |
| MAX_INDEXED_FETCHES | 3 |
| MAX_CANDIDATES / lead | 6 |
| MAX_SOURCE_RETRIES | 2 |
| per-request timeout | 10 s / 20 s |
| MAX_ATTEMPTS (job) | 3 |
| per-job wall clock | ~60 s (soft) |

Stop when: verdict reached; budget exhausted; contradiction; wall-clock cap (persist, resumable); all sources failed.

---

## 17. Module Structure (REVISED — 5 modules, audit corr. 14)

```
app/person_research/
  __init__.py
  models.py        # enums, PersonCandidate, ResearchEvidence, ResearchResult
  scoring.py       # Step A blockers + Step B score + Step C boolean verdict
  sources.py       # ScopedWebsiteSource + IndexedWebSource (+ adapters), behind a protocol
  store.py         # SQLite: jobs table + domain/query/verdict caches + append-only evidence
  orchestrator.py  # Stages 0-2 + candidate decisions + ResearchApplier integration
scripts/research_queue.py   # CLI runner (enqueue / process / report)
```

Dropped from prior draft: separate `cache.py` + `queue.py` (folded into `store.py`), `evidence.py` (folded into `models.py`), and `LeadershipDiscovery` from V1.

---

## 18. Future Extension Boundary (Stage 3/4 later, no redesign)

- `sources.py` Source protocol → adding `LinkedInSource` / `DeepResearchSource` is a new class.
- Waterfall escalation data-driven (`STAGE_ORDER` + per-stage exit/escalate predicates) → inserting Stage 3/4 is config.
- Verdict/score model unchanged; new sources add evidence kinds + authority weights only.
- Social in Stage 3 stays **supporting-only** (never authority), per hard rule #7.

---

## 19. FINAL IMPLEMENTATION CONTRACT

### MUST implement (V1)
- `app/person_research/`: models, scoring, sources, store, orchestrator (+ `scripts/research_queue.py`).
- Two **separate** enums: ResearchStatus `pending|researching|completed|failed`; AttributionVerdict `unattributed|candidates_found|attributed|unresolved`.
- **Per-eligible-email** research unit; skip already-`person_bound`; no cross-email attribution.
- Stages 0 (triage), 1 (scoped company website), 2 (indexed web) only.
- **Boolean rule-first verdict** (Step A blockers → Step C binding); score ranking-only.
- Evidence: mandatory source_url + snippet; append-only; no full-page storage.
- SQLite store (jobs + caches); email-hash identity; domain-hash website reuse; query-hash search cache; `research_version` vs re-scoring split.
- Deterministic failure semantics; unresolved preserved; no lead lost.
- Execution budget caps (§16).
- Deterministic test suite (fixtures only, no live web).
- `ResearchApplier` adapter (in package) — the only writer of person/person_bound.
- Reuse `PeopleParser.extract_candidates`, `role_relevance`, `is_free_mail_domain` via adapters.

### MUST NOT implement
- No social/professional (Stage 3) or deep research (Stage 4).
- No username/domain inference, no guessing, no fabricated evidence.
- No auto-bind on ambiguity; contradiction always → unresolved.
- No scheduler/cron auto-re-research in V1.
- No generic crawler.
- No direct Lead mutation by research (applier only).
- No `LeadershipDiscovery` in V1.

### Existing files that MUST NOT be modified
`pdf_plan_holder_parser.py` (A/B/C/D1), `acceptance_gate.py`, `lead_models.py`, `lead_pipeline.py`, `leadership_discovery.py`, `people_parser.py`, `run_leads.py`, and all Phase A/B/C/D1 tests.

### New files allowed
Everything under `app/person_research/` + `scripts/research_queue.py` + new tests under `tests/person_research/`.

### V1 stages
Triage → Scoped Company Website → Indexed Web → verdict (attributed / candidates_found / unattributed / unresolved), unresolved preserved.

### Attribution rules (binding requires ALL)
No hard blocker ∧ tight co-occurrence ∧ local-part matches co-located name ∧ (independent corroboration ≥ 1 ∨ authoritative company-site pairing) ∧ no contradiction.

### Completion criteria
- All hard rules (§11) enforced and covered by passing tests.
- An unattributed email yields `candidates_found`/`unattributed`/`unresolved`, never a false person.
- Same-domain batch performs one website crawl.
- Crash mid-run resumes without redoing completed stages; no lead lost.
- Already-`person_bound` emails never overwritten.
- Phase A/B/C/D1 and full existing suite remain **green** (no regressions).
- Ruff clean on all new files.
