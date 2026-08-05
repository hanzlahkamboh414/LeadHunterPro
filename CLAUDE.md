# ======================================================================
# LEADHUNTER PRO - CRITICAL PROJECT RULES (NEVER VIOLATE)
# ======================================================================

## 1. Fake Data Policy

Fixtures are TEMPORARY BRIDGE DATA ONLY.

Fixtures must NEVER become the primary discovery source.

Always attempt LIVE discovery first.

Only use fixtures when ALL live discovery methods fail.

Whenever fixtures are returned, the response MUST clearly include:

- data_source = "fixture"
- bridge_mode = true
- fallback_reason

Never hide that fixture data is being used.

---

## 2. Discovery Priority

The discovery pipeline must always follow this order:

User Query
↓
Search Strategy
↓
Live Discovery
↓
Website Crawl
↓
Company Extraction
↓
Company Validation
↓
Ranking
↓
Results

Do not bypass this pipeline.

---

## 3. Search Philosophy

LeadHunter Pro is NOT a Search API client.

LeadHunter Pro is a REAL company discovery platform.

Search APIs (Brave, SearXNG, Google CSE, Bing, etc.) are OPTIONAL discovery sources.

They must NEVER become the core architecture.

The discovery engine must continue working even if one provider disappears.

---

## 4. Architecture Rule

Business Logic MUST NEVER depend on one provider.

Correct:

Discovery Engine
↓
Source Orchestrator
↓
Multiple Sources
↓
Crawler
↓
Extractor
↓
Results

Incorrect:

Connector
↓
Brave API
↓
Results

Every discovery source must be replaceable.

---

## 5. Registry Rules

If no search providers are registered:

DO NOT silently return fixtures.

Always log:

NO SEARCH PROVIDERS REGISTERED

Also log WHY live discovery failed.

---

## 6. Logging Standard

Every discovery execution MUST log:

- Providers Found
- Provider Selected
- Search Query
- URLs Returned
- Companies Crawled
- Companies Accepted
- Companies Rejected
- Validation Results
- Ranking Results
- Fallback Reason

Never log only "live=False".

---

## 7. Root Cause Policy

Never fix symptoms first.

Always identify:

- Root Cause
- Execution Path
- Failure Point
- Why It Happened
- Permanent Fix

Temporary fixes are not acceptable unless explicitly requested.

---

## 8. Search Provider Rule

Search providers are infrastructure only.

They DO NOT define discovery quality.

Discovery quality comes from:

- Search Strategy
- Crawling
- Extraction
- Classification
- Validation
- Ranking

NOT from Brave, SearXNG or any single provider.

---

## 9. Before Every Sprint

Before writing production code, answer:

1. What problem is being solved?
2. Is this solving the root cause?
3. Does this increase technical debt?
4. Can the architecture be simplified?
5. Does this move LeadHunter Pro closer to REAL company discovery?

If the answer is NO,

STOP.

Review the architecture first.

---

## 10. Permanent Goal

The Execute button must eventually perform REAL discovery.

Target flow:

Search Internet
↓
Find Companies
↓
Visit Websites
↓
Extract Company Information
↓
Extract Decision Makers
↓
Validate
↓
Rank
↓
Return Results

It must NEVER become:

Read Fixture JSON
↓
Return Cached Companies

Fixtures exist only as an emergency bridge.

---

## 11. Founder Rule (Highest Priority)

Whenever multiple implementation paths exist:

Choose the solution that makes LeadHunter Pro a long-term production-grade company discovery platform.

Never choose the easiest implementation if it weakens the architecture.

If an architectural decision is uncertain:

STOP implementation.

Explain the trade-offs.

Wait for approval before writing production code.

---

## 12. Never Repeat These Mistakes

The following issues have already occurred and must never happen again:

- Wrong module imports
- Hidden fallback to fixtures
- Fake or placeholder (.example.com) domains
- Empty provider registry without clear diagnostics
- Silent fallback to cached data
- JSON corruption in production fixtures
- Hardcoded connector dependencies
- Provider-centric architecture
- Fake "live" mode
- Returning synthetic companies as production results

Any future implementation must prevent these issues by design.s

# Never implement more than one migration phase at a time.

After each completed phase:

- stop automatically
- summarize work
- report changed files
- report test status
- wait for user approval

Never continue to the next phase without explicit approval.

## 13. Git Safety Rule

Claude must NEVER:

- reset
- restore
- checkout
- revert
- clean
- force push
- delete commits

unless the user explicitly approves.

Before any destructive Git action:

1. Explain the impact.
2. Explain what will be lost.
3. Wait for approval.

Never assume rollback is safe.

## 14. Production Code Rule

Before creating any new module, class, service, or abstraction:

1. Search the repository for existing implementations.
2. Reuse existing production code whenever possible.
3. Do not duplicate functionality.
4. If duplication is unavoidable, explain why.

Existing production code is always preferred over creating new code.

## 15. Engineering Audit Rule

Whenever a major architectural change is proposed:

Do NOT implement immediately.

First produce:

- Root cause analysis
- Existing reusable components
- Duplicate components
- Dead code
- Migration strategy
- Risks
- Rollback strategy

# 16. Implementation begins only after explicit approval.

# Manual Verification Mode (Permanent Rule)

When Bash / PowerShell execution is unavailable or the safety classifier blocks command execution:

## 1. STOP immediately
- Never retry the same command.
- Never attempt another Bash command.
- Never continue implementation.

## 2. Send a Verification Card instead of plain text

Every verification step MUST be presented in this format:

====================================
PHASE X.X — STEP N/TOTAL
====================================

Purpose:
<one sentence explaining why this test matters>

Command:
<exact command to run>

Expected PASS:
<what success looks like>

Paste Result Below:
--------------------------------
<paste terminal output here>
--------------------------------

Status:
WAITING FOR USER

Do not continue until the user provides the result.

## 3. Sequential verification

Only ONE verification step may be active.

Never send Step N+1 before Step N has been verified.

After receiving the user's result:

- Analyze it.
- Explain PASS / FAIL.
- If FAIL:
  - Explain the root cause.
  - Fix only the proven issue.
  - Generate ONE new verification card.
- If PASS:
  - Mark the step complete.
  - Generate the next verification card.

## 4. Never restart the verification session

Maintain one continuous checklist throughout the conversation.

Example:

✅ Step 1 — Import Check
⬜ Step 2 — Plugin Tests
⬜ Step 3 — Discovery Tests
⬜ Step 4 — Connector Tests
⬜ Step 5 — Ruff
⬜ Step 6 — Smoke Test

Only update this checklist.

Never create a new verification session.

## 5. Bash failure rule

If Bash/classifier is unavailable:

DO NOT retry.

Immediately generate the Verification Card for manual execution.

Wait.

## 6. No implementation while verification is pending

Until the current verification step is confirmed PASS:

- Do not implement new code.
- Do not refactor.
- Do not continue to the next phase.
- Do not claim verification without evidence.

## 7. Proof Requirement (Permanent)

Every implementation must include:

- Files created
- Files modified
- Why each file exists
- Architecture impact
- Assumptions made
- Deviations from existing architecture
- Manual verification commands
- Verification checklist
- WAIT FOR USER

Implementation is never considered complete until all verification steps are confirmed.