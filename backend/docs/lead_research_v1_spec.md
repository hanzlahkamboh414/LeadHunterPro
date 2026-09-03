# AI Lead Research Agent — V1 Spec

> Additive side-band that researches a single company email+domain (e.g. from a
> plan-holder PDF, `person=None`) and produces a **LeadDossier**: refined
> company, attributed person, buying-intent, timing, fit for The Best Estimator
> LLC, and a potential-lead score — all with source-cited facts and reasoned
> analysis. Builds on (reuses, never edits) `app/person_research`, the AI
> RouterProvider, the search-provider registry, and the deterministic gate.
>
> **The one principle:** facts are cited or unverified; analysis is reasoned and
> scored; the deterministic gate remains the final authority on "qualified".

## 1. Problem

Plan-holder PDFs yield company emails with `person=None`. Deterministic
person_research only binds when the email co-occurs with a name on the company
site — so real records often stay `unattributed` (dirty domains, initial-last
local parts, no site co-occurrence). The AI agent adds *judgment* on top: it can
read messy pages/directories/PDFs, fix dirty domains, and reason about buying
intent and timing.

## 2. Input / Output

```
Input:  email + domain (from a bid/plan-holder record)
Output: LeadDossier (refined company + person + intent + timing + fit + score)
```

## 3. Pipeline

```
Stage 0  triage          → skip free-mail / generic local-part (reuse)
Stage 1  refine/company  → AI normalizes domain, finds real company + facts (CITED)
Stage 2  person          → person_research (deterministic) + AI augmentation (CITED)
Stage 3  intent/timing   → AI reasoned: needs estimation? if yes, when?
Stage 4  fit/score       → AI + deterministic gate → potential_score + recommendation
```

## 4. File Structure (additive — frozen code zero-touch)

```
backend/app/lead_research/
  __init__.py
  models.py            # LeadDossier, AIEvidence, CompanyProfile,
                       #   PersonFindings, IntentAssessment, TimingAssessment
  prompts.py           # strict prompts (facts=cited, analysis=scored)
  company_research.py  # AI refine domain + company facts (cited)
  person_research_ai.py# person attribution via AI + reuse person_research
  intent_timing.py     # buying-intent + timing analysis (reasoned)
  scoring.py           # potential_score + recommendation (AI + gate)
  agent.py             # AILeadResearchAgent — pipeline orchestration
  service.py           # LeadResearchService — store + agent + persist
backend/scripts/research_lead.py       # CLI runner
backend/tests/lead_research/           # deterministic tests (injected AI + fake search)
backend/docs/lead_research_v1_spec.md  # this file
```

## 5. Data Model — LeadDossier

```python
AIEvidence:        claim, source_url, source_type, confidence(verified|unverified)
CompanyProfile:    name, industry, location, website, facts[AIEvidence]
PersonFindings:    name, role, role_relevance, bound, evidence[cited|unverified]
IntentAssessment:  needs_estimation(yes/no/unknown), signal, reason, evidence
TimingAssessment:  window(now/soon/later/unknown), reason, events
LeadDossier:       email, domain, refined_domain, refined_company,
                   company, person, intent, timing, fit, potential_score,
                   recommendation(contact_now/nurture/skip),
                   sources_checked, source_errors
```

## 6. Safety Rules (non-negotiable)

| Concern | Rule |
|---|---|
| **Facts** (name/role/company facts) | AI MUST cite a `source_url`. No source → `unverified`, never confirmed. |
| **Analysis** (need/timing/fit/score) | AI reasoned judgment over gathered evidence — scored, not fabricated. |
| **Final authority** | Deterministic gate (person_bound email + traceable evidence + role_relevance) still decides "qualified". AI score never bends the gate. |
| **Never invent** | A person name is never derived from an email local-part alone. |

## 7. Reuse (no duplication)

- `app/ai` — RouterProvider (proven live: `ROUTER_OK`, agnes-2.0-flash)
- `app/search_providers/registry.py` — Tavily/Brave/SearXNG
- `app/email/domain_verifier` + `email_cleaner.is_free_mail_domain`
- `app/person_research` — verdict engine, store, LocalPartMatcher
- `app/engines/lead/lead_models` — role_is_plausibly_relevant, generic local parts

## 8. Increments (one at a time — CLAUDE.md §12)

| Inc | Deliverable |
|---|---|
| A | models + prompts + company/domain refine (AI cited) |
| B | person research (deterministic + AI augment) |
| C | intent + timing analysis |
| D | score + recommendation + service + CLI + wiring |

Each increment: deterministic tests (injected AI + fake search), full suite,
Ruff, frozen-file check, and a live proof on real records. Stop + report + wait
for approval after each.
