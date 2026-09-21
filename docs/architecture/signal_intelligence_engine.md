# Company Signal Intelligence Engine — architecture (FROZEN)

Status: **APPROVED — architecture frozen by the founder, 2026-09-18.**
Source: `check.txt` (founder draft, 2026-09-18).
Audit prepared per CLAUDE.md §15; implementation per §16 (explicit approval),
one phase per approval.

This document is now the **specification**, not a proposal. The five open
questions it originally raised (§13 of the first revision) were answered by the
founder and are recorded below as **binding decisions**. Where a decision
changed the design, the change is marked **[FROZEN]**.

---

## 0. The frozen decisions

Five corrections, plus one new invariant. These are constraints, not
preferences — later phases are built on them.

| # | Decision | Effect on this document |
|---|---|---|
| 1 | **Separate `research_evidence.db`, and the domain is NOT the identity.** Use `company_id` (stable internal) + `company_key` (normalized domain, lookup handle) + `company_domains` (aliases/history). | §7.1, §7.2 rewritten |
| 2 | **Converge the three evidence models through adapters**, not a rewrite. Path: existing plugin evidence → adapter → canonical → store → event engine. | §4, §7.3 written as the adapter path |
| 3 | **Split `bid_award` in Phase 0**, migration-safe (old value → classifier → precise event), with tests that enforce no silent promotion. **Reviewed 2026-09-18: the target is `needs_resolution`, NOT `bid_submitted`** — an unestablished stage is its own honest state, not the weakest stage. | §9.1 added |
| 4 | **Two AI calls, and AI proposes while a deterministic engine decides.** AI Call #2 may NOT re-search raw web pages. | §8 rewritten |
| 5 | **Permits: framework in Phase 1, city coverage later.** `permit` stays a schema capability, `permit_issued` stays in the taxonomy. | §12 Phase 1 scope |
| 6 | **[FROZEN] `NOT_FOUND`, `NOT_ACCESSIBLE`, `NOT_VERIFIED`, `VERIFIED` must never collapse into one status.** | §9.5, and enforced in code |

**Data boundary [FROZEN]:**

    dossiers.db           user/business dossier state   — never touched by this engine
    research_evidence.db  evidence · events · projects · signals · pain · runs
    source_scout.db       source registry · source health · verdict ledger
                          — NOT duplicated here

**Why the boundary matters:** a dossier is user/business state; evidence is
disposable, regenerable intelligence. Re-research, reprocessing, dedup or a
pipeline migration must never put a dossier record at risk. One file per
concern also makes the blast radius of a bad migration a file delete rather
than a data loss.

**Layer order [FROZEN]:**

    L0  Identity Resolver
    L1  Research Planner
    L2  Existing Source Registry
    L3  Existing Query / Discovery
    L4  Existing Intent Plugins
    L5  Canonical Evidence Store
    L6  AI Call #1 — Evidence → Events
    L7  Deterministic Event Normalization          ← new since the proposal
    L8  Deterministic Signal Engine
    L9  AI Call #2 — Events → Candidate Pain / Signals
    L10 Deterministic PAIN_GATE
        → Trigger + Outreach

---

## 1. Verdict

**Buildable.** And materially cheaper than it looks, because **roughly two
thirds of the foundation already exists in this repository**, most of it
written for other purposes and never connected to the research pipeline.

`check.txt` reads as a from-scratch design. It is not one. The repo already
contains: a source registry with a staged-trust status model, a source-tier
enum, field-level evidence with confidence, an `IntentEvidence` contract with a
mandatory `source_url`, three working buying-intent plugins (federal awards,
news, company site), a deterministic qualification gate that returns *every*
unmet requirement, a per-stage layered prompt set, and a provenance guard that
already enforces "a search result is not evidence."

What does **not** exist is the middle of the chain: the stored evidence record,
the event layer, the signal layer, the pain layer, and the trigger.

    identity ──▶ evidence ──▶ event ──▶ signal ──▶ pain ──▶ trigger ──▶ outreach
       EXISTS      PARTIAL      NEW       NEW       NEW       NEW        PARTIAL

---

## 2. Root cause of the current weakness (§7)

Not "the AI is bad." The current pipeline hands the model **anonymous text and
asks it to conclude**, then stores the conclusion as prose.

1. `company_research` gathers six blobs of page text; `person_research_ai`
   gathers a few more. Neither is stored as a *record* — they end up as
   `AIEvidence.claim` strings inside `dossier_json`.
2. `IntentAssessment` / `TimingAssessment` ask the model directly: "does this
   company need estimation, and when?" That is a **conclusion without a
   derivation**. There is no intermediate object proving *why*, so the verdict
   cannot be recomputed, audited, or contradicted.
3. Nothing is dated. `AIEvidence` has no `published_at`, so a 2019 award and a
   last-week award are indistinguishable to every consumer — including the
   email writer.
4. Nothing is separated. `IntentEvidenceType.bid_award` collapses *bid
   submitted*, *apparent low bidder*, and *contract awarded* into one value —
   the exact conflation `check.txt` §10 forbids.
5. Nothing accumulates. Evidence is per-lead; two leads at the same company
   cannot share what was learned about that company.

So the failure is structural: **the pipeline stores verdicts, not the
derivation of verdicts.** `check.txt` §36 is right that this is a layer
problem, not a prompt problem.

---

## 3. What already exists (reuse map, §14)

Verified by reading the code.

| `check.txt` section | Existing asset | State |
|---|---|---|
| §4 source registry | [`app/source_scout/store.py`](../backend/app/source_scout/store.py) — `sources` table, `_V2_COLUMNS`, seed upsert | **EXISTS** (for crawl/phone sources) |
| §5 source hierarchy | [`app/engines/verification/source_tiers.py`](../backend/app/engines/verification/source_tiers.py) — `SourceTier` 1–4 + `tier_for_source()` | **EXISTS**, 4 tiers not 5 |
| §6 query generation | `query_expansion.py`, `template_generation.py`, `lead_research/query_learning.py`, `discovery/yield_learning.py` | **PARTIAL** — machinery yes, award/bid/hiring query families no |
| §7 search ≠ evidence | [`lead_research/provenance.py`](../backend/app/lead_research/provenance.py) — `guard_evidence_location()`, `labeled_page_block()` | **EXISTS** |
| §8 evidence object | `AIEvidence`, `FieldEvidence`/`EvidenceSet`, `IntentEvidence` | **THREE partial models → converged in Phase 0** |
| §9 event engine | — | **NEW** (Phase 2) |
| §10 award ≠ bid | `IntentEvidenceType.bid_award` merged them | **SPLIT IN PHASE 0** |
| §13 signal weights | `lead_research/scoring.py` scores *leads*, not signals | **NEW in this shape** (Phase 3) |
| §14 recency | `intent_timing.py` → `TimingAssessment.window` | **PARTIAL** — no day buckets |
| §15–§17 pain | `IntentAssessment.needs_estimation` is the closest thing | **NEW** (Phase 4) |
| §18 contradiction | — | **NEW** (Phase 3) |
| §19 dedup | `SourceOrchestrator._deduplicate`, `dossiers.email_hash` | **PHASE 0 adds project + content keys** |
| §21 permits | `IntentEvidenceType.permit` declared, no plugin | **TAXONOMY IN PHASE 0**, coverage later |
| §22 job board | `IntentEvidenceType.hiring` declared, no plugin | **DECLARED, NOT IMPLEMENTED** |
| §23 person separate | `person_research_ai.py` (710 lines) vs `company_research.py` (866) | **DONE** |
| §24 safe phrasing | `campaigns/personalize.py`, `templates.py`, `spamcheck.py` | **PARTIAL** — no strength-tiered layer |
| §25/§26 UI + Why | `LeadDetail.tsx` + `LeadDetail` type already render `facts[]`, `intent.evidence[]`, `timing.events[]` | **PARTIAL** |
| §27 staged budget | `HOMEPAGE_PRE_VERDICT`, `SEARCH_CACHE_*`, `MAX_ACTIVE_JOBS` | **EXISTS** — already the architecture |
| §29 source health | `source_scout/store.py` V2: `untried→probing→adapter_draft→probation→promoted`, plus `blocked`/`dead`/`exhausted`, with `verdicts` as the success-rate source of truth | **EXISTS — near-exact match** |
| §31 four honest states | `SourceStatus` (SUCCESS/EMPTY/UNAVAILABLE/ERROR) | **PARTIAL** — per-source axis, same doctrine; the research axis is now `ResearchState` |
| §33 DB structure | one-SQLite-file-per-concern + per-dossier JSON | **PATTERN EXISTS** |
| §34 layered prompts | `lead_research/prompts.py` — 7 stage-specific functions | **DONE** |
| §35 PAIN_GATE | [`Lead.qualification_gate()`](../backend/app/engines/lead/lead_models.py) returns `LeadGateResult(qualified, blocked_by[])` | **EXACT TEMPLATE** |

The three highest-value finds:

- **`IntentEvidence` already requires `source_url`** and already enumerates
  `bid_award / hiring / project / expansion / news / permit`. `check.txt` §8's
  evidence object is this contract, one level deep.
- **`Lead.qualification_gate()` is the deterministic-gate pattern** `check.txt`
  §35 asks for, including the "report every unmet requirement" behaviour.
- **The source health model is already built.** `check.txt` §29 describes
  `healthy/degraded/blocked/dead`; the repo has an 8-state model with re-arming
  and a `verdicts` ledger. Building a second one is a review blocker.

---

## 4. Duplicates found, and the convergence path [FROZEN]

Three evidence models with overlapping intent:

1. `app/lead_research/models.py::AIEvidence` — claim / source_url / source_type
   / confidence / source_note.
2. `app/discovery/website/evidence.py::FieldEvidence` + `EvidenceSet` — field /
   page_url / selector / method / confidence / snippet / extracted_at.
3. `app/engines/lead/lead_models.py::IntentEvidence` — type / source_url /
   snippet / date / source / fetched_at.

(There is a **fourth**, record-level model at
`app/engines/source_connectors/evidence.py`; its docstring already explains why
it is separate. It is out of scope for convergence.)

**The frozen path is a projection, not a rewrite:**

    existing plugin evidence → adapter → canonical evidence → evidence store → event engine

`app/research/adapters.py` is the **only** module that knows how an old record
maps onto a new one. Every mapper is additive and side-effect free, which is
exactly what makes switching on the dormant USAspending / Google News /
company-crawler plugins safe: they keep returning the shape they always
returned, and one call at the boundary converts it.

Nothing is deleted. The three existing classes stay as the interfaces their
current callers depend on; retiring them is a separate, separately-approved
step (CLAUDE.md §13).

---

## 5. Dormant code (built, tested, never wired)

- `app/discovery/intent/` — `company_site`, `google_news`, `usaspending`
  plugins, all functional, all tested
  (`tests/discovery/test_intent_registration.py`).
- `app/engines/lead/lead_pipeline.py` + `Lead` + `IntentEvidence` + the
  deterministic gate.

`register_intent_plugins()` is called **only from tests** — never at startup,
never from `app/leads/pipeline.py`. `LeadPipeline` is imported only by
`run_leads.py`, `diagnose_lead_evidence.py`, `scripts/measure_plan_holder_impact.py`
and its own test.

This matches the recorded plan (increment 0 shipped, increment 1 prepared but
not started) — so it is **deliberate dormancy, not a defect**. But it changes
the cost of this project: the source layer is not "to be written", it is **to
be switched on**. That is why Phase 1 is expected to carry the first measurable
ROI.

---

## 6. Target architecture (L0–L10, FROZEN)

Each layer names the module that implements it.

    LEAD (email + registered domain)
      │
      ▼
    [L0] Identity Resolver ......... app/engines/verification/* (EXTEND)
      │      identity_verifier / location_verifier / industry_verifier
      │      + email → person? → role?
      │      → IdentityVerdict{person, company, role, decision_maker}
      │      creates/looks up the STABLE company_id (Phase 0 store)
      │
      ▼
    [L1] Research Planner .......... NEW  app/research/planner.py
      │      inputs: industry, jurisdiction, size, known role
      │      output: candidate (source_id, query_family, bucket) triples
      │      sources come FROM the registry — the planner never invents one
      │
      ▼
    [L2] Source Registry ........... app/source_scout/store.py  (REUSE)
      │      + research-source seeds (awards / permits / jobs / news / procurement)
      │      status model reused verbatim — no second health system
      │
      ▼
    [L3] Query / Discovery ......... app/search_providers/*, app/discovery/*  (REUSE)
      │      query FAMILIES as a checked-in seed list (deterministic)
      │
      ▼
    [L4] Intent Plugins ............ app/discovery/intent/*  (WIRE UP)
      │      BaseIntentPlugin.collect_evidence() — already the right interface
      │      Tier 1 federal awards ✅  Tier 2 company site ✅  Tier 3 news ✅
      │      later plugins: state/county/city procurement, permits, job boards
      │
      ▼
    [L5] Canonical Evidence Store .. app/research/store.py  (PHASE 0 — DONE)
      │      companies / company_domains / evidence / research_runs
      │      NEVER stores a search snippet as evidence (§7)
      │
      ▼
    [L6] AI Call #1 ................ NEW  app/research/events/  (Phase 2)
      │      Evidence → Events. Input: CANONICAL EVIDENCE ONLY.
      │      Factual extraction + classification, closed enum.
      │
      ▼
    [L7] Deterministic Event Normalization   NEW  (Phase 2)
      │      NO AI. Dedup by project_key, contradiction check, promotion
      │      guard (BID_AWARD_CHAIN), permit ≠ award, event validation.
      │      THE LAYER THAT DECIDES WHAT AN EVENT ACTUALLY IS.
      │
      ▼
    [L8] Deterministic Signal Engine ........ NEW  (Phase 3)
      │      NO AI. Weights, recency buckets, evidence count/independence,
      │      contradiction. Produces observations, not inferences.
      │
      ▼
    [L9] AI Call #2 ............... NEW  app/research/pain/  (Phase 4)
      │      Events → CANDIDATE signals / pain hypotheses + reasoning evidence_ids.
      │      Input: verified events + deterministic metadata + recency + weights.
      │      MAY NOT re-search raw web pages.
      │      It PROPOSES. It does not conclude.
      │
      ▼
    [L10] Deterministic PAIN_GATE . NEW  (Phase 4)
             modelled on Lead.qualification_gate()
             → VERIFIED | LIKELY | UNKNOWN | BLOCKED (+ the not-states)
                   │
                   ▼
             Trigger + Outreach ........ campaigns/* (EXTEND, Phase 5)

---

## 7. Data model

### 7.1 Where it lives, and what identifies a company [FROZEN]

A **separate store: `backend/output/research_evidence.db`** — the existing
one-file-per-concern pattern.

**The identity decision (founder correction, 2026-09-18).** The original design
keyed everything on the normalized domain. That orphans every piece of evidence
the moment a company rebrands or operates two domains. So:

    company_id       stable internal id, NEVER derived from the domain
    company_key      normalized domain — a fast lookup handle, NOT the identity
    company_domains  every domain seen for the company, one flagged primary

A domain change therefore **adds an alias**; nothing is orphaned. This is the
whole reason `companies` and `company_domains` are two tables rather than a
`company_key` column on the evidence row.

Two deliberately conservative choices, recorded so they are not mistaken for
bugs:

- `normalize_company_key()` reuses `app.email.pattern_inference.domain_of()`
  rather than adding a fourth domain parser to the repo (CLAUDE.md §14). That
  helper does not reduce to eTLD+1, so `mail.acme.com` stays distinct from
  `acme.com`. The error direction is safe: two subdomains of one company become
  an extra company (recoverable) rather than two unrelated companies being
  merged (corrupted evidence).
- A domainless company is created only by an explicit `create_company(name)`
  call, never by `resolve_company("")`. Keying a domainless lookup on the name
  would create a duplicate company on every call — the exact defect this store
  exists to prevent.

### 7.2 Tables

**Phase 0 shipped** (`app/research/store.py`):

    companies         company_id PK · company_key · name · created_at · updated_at
    company_domains   domain PK · company_id · is_primary · first_seen_at
    evidence          evidence_id PK · company_id · source_url · source_type ·
                      publisher · title · excerpt · published_at · retrieved_at ·
                      company_match · evidence_type · project_key ·
                      event_candidate · confidence · legacy_type · verification ·
                      content_hash · UNIQUE(company_id, content_hash)
    research_runs     run_id PK · company_id · state · started_at · finished_at ·
                      coverage_json · honest_reason

**Later phases** (not created speculatively — one phase per approval):

    events            event_id · company_id · event_type · project_key · occurred_at ·
                      confidence · evidence_ids
    signals           signal_id · company_id · signal_type · strength · score ·
                      computed_at · contradiction
    signal_evidence   signal_id · evidence_id · contribution
    pain_hypotheses   hypothesis_id · company_id · pain_type · confidence · verdict ·
                      blocked_by · computed_at
    pain_evidence     hypothesis_id · evidence_id · role
    outreach_triggers trigger_id · company_id · angle · basis · strength

`research_source_registry` and `source_health` are **already served** by
`source_scout.db`. Adding them here is the duplicate-component mistake §14
forbids.

### 7.3 Event vs Signal vs Pain — three tables, not one

A signal is an **observation** ("3 recent awards"), a pain hypothesis is an
**inference** ("estimating capacity pressure, 0.78"), and a trigger is a
**choice** ("lead with additional estimating capacity"). Collapsing them is
exactly the bug being fixed. Three tables, three lifetimes, three confidence
semantics.

### 7.4 The three fields that carry the root-cause fix

`published_at`, `project_key`, `company_id`. The first two were **absent
entirely** from all three legacy models — which is why a 2019 award and a
last-week award were indistinguishable, and why one project on three websites
looked like three projects. `CanonicalEvidence` refuses to be constructed
without a `source_url`, and refuses `company_match=MISMATCH` outright: evidence
about a different company is never stored, so no downstream layer has to
remember to filter it.

`verification` coexists with `confidence` rather than being derived from it.
The legacy models carried tier *labels* ("verified"/"unverified"); the canonical
record carries real numbers where a source states one. Converting a label into a
number would be fabricated precision, so the label is stored verbatim and the
two scales never override each other.

---

## 8. The AI budget, and the propose/decide split [FROZEN]

Two rules govern this section.

**Rule 1 — AI is not used for enumerable things.** Recorded 2026-09-17: an
enumerable list is a seed file. Using AI on it spends credits to get a worse,
non-reproducible version of a list we can write down once.

**Rule 2 — AI proposes; the deterministic engine decides.** Without this split,
`check.txt`'s original defect returns under a new name: *AI says pain → the
system trusts the AI*.

So `check.txt` §34's five agents become **two AI calls per company**:

| `check.txt` agent | Implementation | AI? |
|---|---|---|
| 1 Extractor | AI Call #1: extract + classify, ONE call, closed-enum output | **AI** |
| 2 Classifier | merged into #1 — classification is part of reading the page | — |
| 3 Correlator | AI Call #2: correlate verified events only → candidate signals/pain | **AI** |
| 4 Pain Analyst | merged into #2 — pain is the correlation's *candidate* output | — |
| 5 Outreach Analyst | existing `campaigns/personalize.py` + strength tiers | **AI** (already exists) |

**AI Call #1 — Evidence → Events.** Input: canonical evidence only. Output:
`project_announced`, `contract_awarded`, `apparent_low_bidder`, `estimator_hiring`,
`new_office`, … Factual extraction and classification.

**AI Call #2 — Events → Candidate Signals/Pain.** Input: verified events +
deterministic metadata + recency + existing signal weights. Output:
`candidate_signals`, `candidate_pain_hypotheses`, reasoning `evidence_ids`.

> **AI Call #2 has no permission to re-search raw web pages.** [FROZEN]
> Its input is what the deterministic layers already verified. If it needs
> something the evidence does not contain, the answer is a coverage gap to
> record — not a fetch to make.

**Deterministic code — no AI, ever:**

- **Query families** — a checked-in seed list. "`[company]` apparent low
  bidder" is a string; asking a model to produce strings we can type is waste.
- **Recency buckets** — a pure date function.
- **Signal weights** — arithmetic.
- **Contradiction detection** — set logic over stored events.
- **Project deduplication** — normalized project key.
- **Event normalization and the promotion guard** (L7) — the chain constant.
- **Coverage score** — counting.
- **PAIN_GATE** (L10) — code, mirroring `qualification_gate()`.

The final pain verdict chain is therefore:

    AI candidate → contradiction check → recency check → evidence count/independence
                 → PAIN_GATE → VERIFIED | LIKELY | UNKNOWN | BLOCKED

---

## 9. Deterministic engines (specified, not AI)

### 9.1 The procurement chain and the `bid_award` split [FROZEN, Phase 0]

`bid_award` covered *bid submitted*, *apparent low bidder* and *contract
awarded* in one value. The correct taxonomy — enforced in code, not in a
prompt — is:

    bid_submitted → apparent_low_bidder → recommended_for_award
                  → contract_awarded → contract_signed

and the equalities that must never hold:

    bid_submitted        ≠ apparent_low_bidder
    apparent_low_bidder  ≠ contract_awarded
    contract_awarded     ≠ contract_signed

The migration is **migration-safe**: the old value is not deleted, it is mapped
through `app/research/adapters.py`, and it resolves to
`needs_resolution` — **not to a stage at all**.

**Corrected 2026-09-18 after founder review.** The first implementation resolved
it DOWN to `bid_submitted`, reasoning that under-claiming is survivable. The
direction was right; the floor was wrong:

> **Resolving ambiguity downward is only safe when the weaker claim is still
> TRUE.**

`bid_submitted` is not a weaker version of "bid/award-related evidence" — it is
a *different assertion*. A page can be procurement-related without the company
having submitted anything:

    "Bid documents available; Acme picked up a set."   → no bid submitted
    "Council rejected all bids."                        → nothing submitted at all
    "Acme named as a subcontractor on the award."       → did not bid

Mapping those to `bid_submitted` manufactures a fact, which is exactly the
failure mode this whole engine exists to remove — merely one step further down
than the original `contract_awarded` error. It also violates the never-collapse
rule: *we could not establish the stage* is its own honest state, not a stage.

So the resolved target is `EventType.NEEDS_RESOLUTION`, which:

- carries **no `chain_rank()`** and is a member of neither
  `ACTIVITY_ONLY_EVENTS` nor `CONTRACT_AWARD_EVENTS` — it asserts neither
  activity nor a win, so it can never be counted as either;
- is left only through `may_promote(..., new_evidence=True)` — the stage must be
  **proven from source text** by the event engine (Phase 2), never assumed at
  the adapter boundary;
- is reachable *into* from any stage at any time, because retracting an
  unsupported claim is the honest direction.

This is an architectural fix, not a cosmetic refactor: `BID_AWARD_CHAIN`,
`UNRESOLVED_EVENTS`, `chain_rank()`, `may_promote()` and `promotion_violation()`
are constants and guards, and `check_legacy_mapping()` refuses **every** stage
for `bid_award` — including `bid_submitted` — so the mistake is reachable only
by deliberately deleting the guard. The original string survives in
`CanonicalEvidence.legacy_type`, keeping the migration auditable.

### 9.2 A permit is activity, never a win [FROZEN]

`permit_issued` = project activity evidence. It is **excluded** from
`CONTRACT_AWARD_EVENTS`, so no signal or pain layer can derive "won a bid" from
a permit. `check.txt` §21 states this; the taxonomy hard-codes it.

### 9.3 News is a source kind, not an event

`news` is deliberately absent from the legacy→event map. A newspaper mentioning
a company is a **source kind**; its event must be read off the page by the event
engine, never assumed from the fact of publication.

### 9.4 Recency, contradiction, dedup, coverage

**Recency** — `recency_bucket(occurred_at, now) -> very_recent | recent |
moderate | historical | background` at 0/30/90/180/365 days. Unknown date →
`background`, never `recent`. (Ordering note, already in the store: undated
evidence sorts *last*, never first.)

**Contradiction** — a stored event is flagged `stale` when a *later,
equally-or-more-authoritative* record contradicts it. Two implemented rules:
same `project_key` with a later terminal state supersedes the earlier; an `open`
posting superseded by a `filled`/`closed` observation. Contradicted events are
excluded from signal input and recorded on the signal — never silently dropped.

**Project dedup** — `project_key` from normalized (owner, project name,
jurisdiction). N sources → 1 event, N evidence rows. Deliberately conservative:
an empty name yields `""` rather than a key invented from the owner alone, which
would merge every unrelated project in a city into one bucket.

**Coverage** — per-bucket `(found, searched, not_accessible)` triples.

### 9.5 The four honest states never collapse [FROZEN]

    NOT_FOUND        searched; no relevant evidence exists to cite
    NOT_ACCESSIBLE   the source exists but was blocked / login-walled / errored
    NOT_VERIFIED     evidence exists but conflicts or is incomplete
    VERIFIED         evidence is sufficient and uncontradicted

**"We did not find it" does not mean "it does not exist."** These are four
stored values, not one status with notes. An in-flight run is `running` —
explicitly not one of the four — so an interrupted run can never be misread as
a negative result. Any non-`VERIFIED` state requires an `honest_reason`; the
store refuses to record a bare negative. This is CLAUDE.md §6's "never log only
`live=False`" applied to storage.

**The same rule, one level down: whose fault a failure is** (added in Phase
1.1). `NOT_ACCESSIBLE` covers a blocked source, a 5xx and a timeout alike, and
those are not the same event — one is nobody's fault yet, the other two are
theirs, and a fourth case is OURS: a request we built wrong. That fourth case
does not belong under `NOT_ACCESSIBLE` at all, because a malformed request is
not an accessibility problem, it is a defect, and it stays broken until someone
fixes it. `SourceFailureReason` (`REQUEST_ERROR` / `SOURCE_ERROR` /
`ACCESS_ERROR`) carries that distinction in `metadata["reason"]`, and
`SourceStatus.ERROR` is what a 4xx maps to. The cost of collapsing them is not
hypothetical: this is exactly how the `usaspending` plugin spent its whole life
returning nothing while every status line called it "unavailable" (§12, D2).

### 9.6 PAIN_GATE

```python
@dataclass
class PainGateResult:
    verdict: str          # VERIFIED | LIKELY | UNKNOWN | BLOCKED
    blocked_by: list[str] # EVERY unmet condition, never a silent pass
```

Conditions, all required for `VERIFIED`: ≥1 direct evidence **or** ≥2
independent strong signals; recency within threshold; company identity
`confirmed`; no contradiction; and an **inference-strength check** — the
hypothesis's confidence may not exceed what its evidence licenses. That last one
is the machine-enforced version of *"3 awards alone → UNKNOWN, never
estimating_capacity."*

---

## 10. Wiring into the live pipeline

The single seam: `app/leads/pipeline.py::run_discovery` produces company
records; `AILeadResearchAgent.research()` (agent.py:208) runs the per-lead
stages. The new engine slots in **between** them as a company-scoped step, once
per company, before the per-lead stages — so two leads at one company share one
evidence run.

Three wiring points:

1. `register_intent_plugins()` at startup. `app/main.py` has no plugin
   registration block today; this is the "empty provider registry without
   diagnostics" surface §12 warns about, so registration must **log what it
   registered and why**.
2. `AILeadResearchAgent.research()` — after Stage 0.7 pre-verdict, before
   Stage 1 company research: consult the evidence store; on a miss, run the
   planner → acquisition → evidence → event → signal → pain chain.

   **Amended 2026-09-18 (Phase 1, founder-approved): the hook goes after
   Stage 1c's not-a-client shortcut, not before Stage 1.** The original
   placement above was chosen because the engine is company-scoped — one run
   should serve every lead at the company — and that is still the goal
   *among the leads that survive*. Two things moved it:

   * **Credits.** Before Stage 1 the agent holds only an email and a domain.
     The three plugins make live network calls, so a lead the pipeline is
     about to drop as not-a-buyer would still have paid for three
     collections. That is the founder's recorded biggest leak — pre-verdict
     research on not-our-client leads.
   * **Correctness.** After Stage 1c the researched company name and website
     exist, and the plugins need them: `collect_evidence(company_name=...)`
     against an email-derived guess searches the wrong entity. A real site
     also yields the `company_page` evidence that a domain alone cannot.

   The cost, stated plainly: the run is no longer once-per-company across
   *all* leads — it is once-per-company across *surviving* leads. The
   company-keyed store makes the repeat cheap rather than duplicate: a second
   run over the same company stores zero new rows and reports them as
   already known (§7.1). The hook is also injectable
   (`AILeadResearchAgent(intent_evidence_plugins=...)`) so tests run offline,
   matching the repo rule that every network stage sits behind a seam.

3. `dossier_json` — gains a `signal_intelligence` block carrying the trigger,
   pain hypotheses, coverage and evidence ids. Additive: old dossiers simply
   lack the key, and `LeadDetail.from_dict` already tolerates missing fields.

---

## 11. UI delta (`check.txt` §25/§26)

`LeadDetail.tsx` already renders `company.facts[]`, `intent.evidence[]` and
`timing.events[]` — so the "evidence with a source" muscle exists. New:

- **RECENT ACTIVITY** — dated events with relative ages.
- **SIGNALS** — `HIGH/MEDIUM/LOW` per signal family.
- **PAIN HYPOTHESES** — type + confidence %, with unknowns shown *as* unknown.
- **RECOMMENDED ANGLE** + **WHY?** — expands to per-reason source, publisher,
  date. The evidence rows already carry every field this needs.

Two non-negotiable UI rules:

1. A hypothesis with no evidence behind it renders as `Unknown`, never as a
   low-confidence guess.
2. **Every pain shows a "Why?"** → evidence + date + source.

---

## 12. Phase order [FROZEN]

One phase per approval (CLAUDE.md). After each: stop, summarize, report changed
files and test status, wait.

| Phase | Content | AI? | Status |
|---|---|---|---|
| **0** | `research_evidence.db` · canonical evidence · **`bid_award` split** · evidence dates · project keys · company-level accumulation · evidence provenance linkage | no | **SHIPPED** |
| **1** | **Wire what already exists**: USAspending, Google News, company-site crawler; connect `register_intent_plugins()` to the production pipeline. Permit capability stays in the schema and `permit_issued` in the taxonomy; source coverage expands jurisdiction-by-jurisdiction later. | no | **SHIPPED** 2026-09-18 — wiring live-verified; **1.1** fixed the two source defects it exposed |
| **2** | Event engine: AI Call #1 + deterministic event validation (L6 + L7) | yes (1 call) | **SHIPPED TO TEST** 2026-09-20 |
| **3** | Signal engine + correlation + weights (L8) | no | **SHIPPED TO TEST** 2026-09-21 |
| **4** | Pain engine: AI candidate hypotheses (L9) + deterministic PAIN_GATE (L10) | yes (1 call) | **SHIPPED TO TEST** 2026-09-21 |
| **5** | Outreach trigger + strength-tiered phrasing + UI with per-pain "Why?" | yes (existing) | not started |

**Phase 1 is the first measurable ROI**, because no new source technology has to
be developed — an existing acquisition capability is switched on.

### Phase 0 — shipped artifacts

    backend/app/research/__init__.py     package doc, layer map, exports
    backend/app/research/taxonomy.py     event/evidence vocabulary + chain guard
    backend/app/research/models.py       CanonicalEvidence, CompanyIdentity, keys
    backend/app/research/adapters.py     the three legacy → canonical mappers
    backend/app/research/store.py        ResearchEvidenceStore (separate DB)

    backend/tests/research/test_taxonomy.py  the §10 + §21 prohibitions
    backend/tests/research/test_models.py    the canonical record's contract
    backend/tests/research/test_adapters.py  the bid_award split
    backend/tests/research/test_store.py     identity, dedup, the four states

    backend/tests/conftest.py            + autouse isolation of the real DB

**One Phase 0 correction, recorded for audit** (founder review, 2026-09-18): the
`bid_award` adapter first targeted `bid_submitted`. That was corrected to
`needs_resolution` before Phase 1 began — see §9.1. No stored data is affected
(the adapters are unshipped and nothing was migrated), and
`test_adapters.py::test_intent_evidence_refuses_a_hand_edited_floor_too` now
pins the original mistake so it cannot be re-introduced.

### Phase 1 — shipped artifacts

    backend/app/research/intake.py            the L0→L4→L5 bridge + honest-state classifier
    backend/app/research/__init__.py          + CompanyIntakeResult, collect_company_evidence
    backend/app/discovery/intent/__init__.py  default_intent_plugins, registered_intent_plugins,
                                              IntentCollection, collect_intent_evidence
    backend/app/engines/lead/lead_pipeline.py re-exports default_intent_plugins; _collect_intent
                                              now delegates (no duplicated loop, §14)
    backend/app/lead_research/agent.py        the hook after Stage 1c + the injectable
                                              intent_evidence_plugins seam
    backend/app/core/config.py                INTENT_EVIDENCE_ENABLED (default ON, kill-switch)
    backend/app/main.py                       startup registration, with the §5 diagnostic

    backend/tests/research/test_intake.py                  the four states, identity, dedup
    backend/tests/discovery/test_intent_collection.py      the extracted collection loop
    backend/tests/lead_research/test_agent_intent_evidence.py  WHERE the hook runs, and where not
    backend/tests/conftest.py                              autouse: no test hits a live endpoint

    backend/scripts/intent_evidence_check.py  the post-deploy gate (exit 2 on silence only)

### Phase 1.1 — the two source defects (approved and shipped 2026-09-18)

    backend/app/discovery/sources/status.py      + SourceFailureReason (whose fault?)
    backend/app/discovery/intent/company_site.py + phrase frequency, chrome exclusion
    backend/app/discovery/intent/usaspending.py  + required request keys, failure split
    backend/app/research/intake.py               failure wording: never "unreachable"
                                                 for a request WE got wrong

    backend/tests/discovery/test_company_site_intent.py  + TestSiteChrome (4)
    backend/tests/discovery/test_usaspending.py          + required-field and
                                                         failure-attribution pins
    backend/tests/fixtures/intent_pages.py                + CHROME_SITE fixtures

**Live verification, 2026-09-18** (`intent_evidence_check.py --domain
turnerconstruction.com --read-back`): state `VERIFIED`; stable
`company_id cmp_18e8d307ce4542d1`; run 1 stored 105 rows, runs 2 and 3 stored
**0** and reported 105 already known — the company-keyed store refining
instead of duplicating is now measured, not asserted. `published_at` is
populated on the news rows, so the §7.4 recency field survives the round trip.

**Two source defects found during that verification, both fixed in Phase 1.1
(approved 2026-09-18). Neither was introduced by Phase 1's wiring — both were
pre-existing plugins — but both fed L5 directly, so fixing them before Phase 2
was the point of the increment.**

* **D1 — `company_site` counted site-wide chrome as page evidence. FIXED.**
  Of 5 `company_page` rows for Turner, 4 (`/`, `/services`, `/projects`,
  `/insights`) carried the *same* excerpt — the careers promo block in the
  global footer: *"fulfilling career awaits you at Turner! Join our team…"*.
  The cause: `text_content` is `soup.get_text()`, chrome included, and the old
  `_evidence_from_page` scanned title + description + text as one blob, so
  template chrome was indistinguishable from page content. Fix, deterministic
  and model-free: a phrase carried by **more than half** the pages already
  fetched (and only when at least `MIN_PAGES_FOR_CHROME = 3` pages were read)
  is the template talking, not the page. It is dropped per **phrase**, not per
  page, so a page that says "Now hiring" in its own words keeps its evidence
  even while it also matches the footer — which is what makes the rule
  subtract chrome instead of subtracting signal. The parser was deliberately
  NOT touched: it is shared with the discovery crawler, whose address,
  leadership and services extractors read the same field. Exclusions are
  reported in the plugin's metadata (`chrome_phrases`, `chrome_matches`) and
  named in the `EMPTY` note, so suppression is never silent.
* **D2 — `usaspending` had never returned a single row. FIXED.** The request
  body omitted `filters.award_type_codes` and `subawards`, both required by
  the endpoint. Probed live: as shipped → **422**, with the API's own message
  missing value: `filters|award_type_codes` is a required field; with those
  keys added → **200**, returning e.g. `W9128F23C0023 / TURNER CONSTRUCTION
  COMPANY / $393,700,142`. The plugin's `UNAVAILABLE` reporting is what kept
  the failure honest instead of silently reading as "no federal awards" — but
  it was also what HID it, since "unavailable" reads as a passing outage
  rather than a permanent defect of ours. So the fix is two-part: the required
  keys, and a failure vocabulary that says whose fault a failure is
  (`SourceFailureReason`: a 4xx is `REQUEST_ERROR` / `SourceStatus.ERROR`,
  because our malformed request is not the source being down). The API's own
  explanation is now kept in `metadata["detail"]` and logged — on the 422 it
  named the exact missing field, and throwing that away left "unavailable" as
  the only clue anyone could act on.

**Live re-verification after Phase 1.1, same company** — both defects
confirmed fixed end to end:

    company_site   success      returned=1    (was 5 — four were footer echoes)
    google_news    success      returned=100
    usaspending    success      returned=20   (was unavailable, returned=0)

    state=VERIFIED  collected=121  stored=29  known=92  reason="...company_site,
    google_news, usaspending provider(s) (29 new, 92 already known)"

**One honest caveat, recorded rather than tidied away:** the fix stops new
chrome rows; it does not delete the ones already written. Four historical
footer-echo rows for `turnerconstruction.com` are still in the local
`research_evidence.db` (verified by read-back: 4 rows carrying the footer
excerpt). Every run before the fix wrote its own, so a production database
would carry the same residue and any cleanup is a data decision, not a code
one. `dossiers.db` is unaffected — L5 is a new store and nothing reads it yet.

### Phase 2 — shipped to test artifacts (2026-09-20)

    backend/app/research/events/models.py   immutable EventRecord + stable ids
    backend/app/research/events/engine.py   one AI call + deterministic validation
    backend/app/research/events/__init__.py public Phase 2 API
    backend/app/research/store.py           events table + validated persistence
    backend/app/lead_research/agent.py      post-intake hook + unchanged-evidence cache
    backend/scripts/live_event_extraction_check.py  real AI/store/read-back gate
    backend/tests/research/test_events.py   Phase 2 behavioral contract

The deterministic layer accepts only the closed `EventType` vocabulary and
requires every proposed `evidence_id` to be present in the exact company
snapshot sent to the model. Permit-only evidence cannot become an award;
procurement stages require explicit wording or a definitive USAspending award
record; proposed dates must match a source date or appear literally in the
supporting text. Unconfirmed rows are withheld from AI unless the exact company
name appears in their stored text; an own-company-site row is identity-bound by
the acquisition path. This prevents broad-name results such as
`Whiting-Turner` from becoming events for `Turner Construction Company`.

Live test-server verification against `agnes-2.5-flash`: the intake stored 121
raw rows for an exact-name Turner Construction test identity. The identity gate
withheld 93 unrelated/unconfirmed rows. Date validation rejected three
ungrounded proposals. The store accepted 20 distinct `contract_awarded` events,
each linked to one exact-name USAspending record; a read-back found zero links
to `Whiting-Turner`. Re-running unchanged company evidence is skipped on the
production hook, so a second lead at the same company does not spend another
event-extraction call. Remote Phase 2 tests passed 12/12 and the service
returned HTTP 200 from `/api/v1/health` after restart.

### Phase 3 — shipped to test artifacts (2026-09-21)

    backend/app/research/signals/models.py  closed signal/strength/recency records
    backend/app/research/signals/engine.py  deterministic correlation and weights
    backend/app/research/signals/__init__.py public Phase 3 API
    backend/app/research/store.py           signals + signal_evidence persistence
    backend/app/lead_research/agent.py      production scoring hook after events
    backend/scripts/live_signal_scoring_check.py real DB/read-back gate
    backend/tests/research/test_signals.py  Phase 3 behavioral contract

The signal engine does not call AI. It applies the frozen 0/30/90/180/365-day
recency boundaries, preserves bid/award/permit separation, counts one project
once while retaining all supporting evidence, and records excluded stale events
with explicit contradiction reasons. A later procurement stage can supersede an
earlier stage only when it is newer and at least equally authoritative. A
filled/closed job observation removes the older opening from active hiring.
Signals and pain remain separate: Phase 3 stores observations only.

Live test-server verification used the 121 stored Turner Construction evidence
rows and 20 validated award events from Phase 2. Phase 3 persisted two signal
families (`contract_award_activity` and `project_activity`), each linked back to
all 20 exact-company evidence rows. Both scored `MEDIUM` because all stored award
dates were older than 365 days and therefore correctly classified as
`background`; no recent trigger was fabricated. Read-back matched the computed
snapshot, no contradictions were present, remote tests passed 67/67, and the
service returned HTTP 200 from `/api/v1/health` after restart.

### Phase 4 — shipped to test artifacts (2026-09-21)

    backend/app/research/pain/models.py    pain types, candidates and gate verdicts
    backend/app/research/pain/engine.py    AI Call #2 + deterministic PAIN_GATE
    backend/app/research/pain/__init__.py  public Phase 4 API
    backend/app/research/store.py          pain_hypotheses + pain_evidence storage
    backend/app/lead_research/agent.py     cached production pain-inference hook
    backend/scripts/live_pain_inference_check.py real AI/store/read-back gate
    backend/tests/research/test_pain.py    Phase 4 behavioral contract

AI Call #2 sees only stored events and deterministic signal metadata. The prompt
forbids browsing, URL fetching and outside facts; canonical page excerpts are
not supplied to the model. The model proposes one of the eight closed pain
types and cites existing signal/evidence ids. PAIN_GATE then checks every id,
pain-specific relevance, confirmed company identity, 0–90 day recency,
contradictions, direct pain wording or two independent strong signals, and a
deterministic confidence ceiling. The stored verdict is `VERIFIED`, `LIKELY`,
`UNKNOWN` or `BLOCKED`, with every failed condition in `blocked_by`.

Live test-server verification used `agnes-2.5-flash` against Turner's 121 stored
evidence rows, 20 events and two Phase 3 signals. The model proposed three pain
hypotheses, but PAIN_GATE marked all three `BLOCKED`: the events were historical,
the cited stored rows did not carry confirmed identity/confidence sufficient for
pain, and two proposals lacked their required signal family. No historical award
was promoted into a current verified pain. The database read-back matched, and
remote Phase 4 tests passed 71/71.

---

## 13. Conflicts — RESOLVED

The five questions in the first revision were answered by the founder on
2026-09-18 and are now binding (§0). For the record:

1. **17 tables in `dossiers.db` vs a separate DB** → **separate**
   `research_evidence.db`, with a stable `company_id` rather than the domain as
   identity.
2. **Converge three evidence models?** → **yes, via adapters**, in Phase 0 — not
   a rewrite. Existing models stay as their callers' interfaces.
3. **Split `bid_award`?** → **yes, Phase 0**, migration-safe, with tests that
   enforce no silent promotion.
4. **Two AI calls, not five?** → **yes**, with the additional lock that AI
   proposes and the deterministic engine decides, and that AI Call #2 may not
   re-search the raw web.
5. **Permits?** → **framework in Phase 1, coverage later.** Phase 1 is not
   blocked on nationwide permit coverage.

---

## 14. Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| Credit burn | A per-company evidence run is more expensive than the current per-lead one | Company-keyed store: one run serves every lead at that company; two AI calls, not five; deterministic layers gate what reaches AI |
| Source coverage hole | `NOT_FOUND` vs `NOT_ACCESSIBLE` will be confused by users | Four honest states stored, never collapsed (§9.5) |
| Identity drift | Attributing another company's award to this company | `company_match` required; `MISMATCH` rejected at construction; identity resolution before event creation |
| Domain change orphaning evidence | The failure the `company_id` correction exists to prevent | `company_domains` aliases; `company_id` never derived from the domain |
| AI re-introducing the original defect | "AI says pain → system trusts AI" under a new name | AI proposes, deterministic engine decides; Call #2 cannot re-search; PAIN_GATE is code |
| Second health system | Easy to accidentally build one | Reuse `source_scout` `verdicts`; a new health table is a review blocker |
| Scope | 6 phases touching research, storage and UI | One phase per approval, stop-and-report after each |

---

## 15. Rollback

The whole feature is **additive and dark**:

- New store file → delete the file; nothing else references it. `dossiers.db` is
  never opened by this engine.
- New modules → imported by tests only until the phase that wires them.
- `dossier_json.signal_intelligence` → additive key; readers tolerate absence.
- Phase 0's only edit outside `app/research/` is the conftest isolation fixture.
  The `bid_award` split does **not** edit `IntentEvidenceType` — the legacy value
  stays, and the adapter demotes it on the way in, so no shipped caller changes.

No destructive git operation is required or proposed (CLAUDE.md §13).

---

## 16. Verification per phase

1. `python -m pytest` from `backend/` (module flag; bare `pytest` fails here).
2. A live check that exercises the phase's real unit against the network, not a
   liveness probe — the 2026-09-18 outage proved `is-active` / `/health` prove
   nothing. Phase 1's equivalent of `scripts/live_expansion_check.py` is a
   `scripts/live_evidence_check.py` that runs one real company end to end.
3. Honest-artifact review: every `NOT_FOUND` distinguishable from
   `NOT_ACCESSIBLE` in the stored row, and no evidence row without a
   `source_url`.

---

## 17. Recommendation

Phases 0, 1, 1.1, 2, 3 and 4 are shipped to the test server. The next unit is
**Phase 5** — outreach trigger, strength-tiered phrasing and the evidence-linked
Company Intelligence UI — under the one-phase-per-approval rule.
