# LeadHunter Pro — Phase 2 implementation report

Date: 2026-09-20
Target: test VPS only
Main VPS: unchanged

## Delivered

- Deployed the Phase 0/1 canonical evidence prerequisites that were absent on
  the test VPS.
- Added Phase 2 AI Call #1: canonical evidence to factual event proposals.
- Added deterministic validation for the closed event taxonomy, evidence IDs,
  company ownership, company identity, permit/award separation, procurement
  stage proof, source-grounded dates and project/event deduplication.
- Added the `events` table to the separate `research_evidence.db` store.
- Wired event extraction after verified company evidence intake.
- Prevented repeat AI spend when a later lead at the same company produces no
  new evidence.
- Restored the frozen `SourceFailureReason` public name and aligned its tests
  with the documented `ERROR` versus `UNAVAILABLE` contract.

## Automated verification

- Local Phase 2 and adjacent regression set: 325 passed.
- Local lint on every touched Phase 2 and prerequisite file: passed.
- Remote Phase 2 suite on the test VPS: 12 passed.
- A broad local run completed with 3,761 passing tests. Four USAspending tests
  exposed stale expectations and were corrected. Two unrelated order-sensitive
  failures passed in isolation. A later full rerun stalled in an existing
  long-running test and was interrupted; it is not represented as a clean full
  suite.

## Live verification

- Test service restarted successfully and `/api/v1/health` returned HTTP 200.
- Startup registered `company_site`, `google_news` and `usaspending`.
- Exact-name Turner Construction intake stored 121 canonical evidence rows.
- The identity gate withheld 93 unrelated or unconfirmed rows before the AI
  call.
- Date validation rejected three AI proposals whose dates were absent from the
  supporting evidence.
- Twenty `contract_awarded` events were stored from twenty distinct,
  exact-name USAspending award records.
- Read-back found zero event links to `Whiting-Turner` evidence.
- Test-server code backup:
  `/opt/leadhunter/backups/20260920-phase2-before.tar.gz`.
- Pre-date-fix evidence database backup:
  `/opt/leadhunter/backups/research_evidence-before-phase2-date-fix.db`.

## Remaining measured gaps

- Main has two Gmail export routes absent on test:
  `/api/v1/gmail/export/counts` and `/api/v1/gmail/export/progress`. Test has
  14 routes absent on main. Gmail parity is a separate unit and was not mixed
  into the frozen signal-engine phase.
- Phase 3 deterministic signal weights, recency correlation and contradiction
  handling are not implemented yet.
- Phase 4 pain hypotheses and deterministic PAIN_GATE are not implemented yet.
- Phase 5 outreach trigger and advanced evidence/Why UI are not implemented yet.
- The configured background quotas are 5,000 phones and 2,000 emails per day,
  but the live audit has not demonstrated those valid-result volumes. They
  remain throughput acceptance targets, not a completed claim.
