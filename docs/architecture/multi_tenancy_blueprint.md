# LeadHunter Pro — Multi-Tenancy Blueprint

**Status:** Architecture approved for implementation; storage and identity foundations are in progress. Not ready for test-server tenant cutover.
**Prepared:** 2026-09-25.
**Scope:** Convert the current single-installation, user-scoped product into an organization-based SaaS without mixing customer data or duplicating national source discovery.

## Implementation checkpoint (2026-09-25)

- A verified offline SQLite consolidation tool exists. It seeds **The Best Estimators LLC** and maps existing users into its membership table. Source databases remain untouched.
- An opt-in operational path selector refuses missing/unverified unified files; default behavior remains the current separate SQLite files. Store initialization against one marked file and the affected regression suites pass locally.
- Tenant membership storage and a request dependency check live membership, reject an unauthorized tenant selector, and disallow auth-off anonymous access in multi-tenant mode. These foundations are not yet wired into every private API or data query.
- Tenant-mode platform-admin account creation now requires a valid tenant and commits the user and first membership atomically. An authenticated user can list only their current memberships through `/api/v1/auth/tenants`; revocation disappears from that list immediately. Existing single-tenant account creation is unchanged.
- Tenant-mode Admin Operations → Users & Activity now includes a Tenants section. A platform admin can create a tenant (becoming its first owner), view live member counts and names, assign/remove existing member accounts, and select a tenant when creating an account. The final owner cannot be removed or demoted. This UI is hidden in legacy mode and cannot be used on the test server until the isolation deployment gate passes. It tracks actual members; a configurable seat cap is not implemented.
- The offline consolidation maps all six existing private phone tables (claims, saved contacts, claim history, user limits, call events, wrong-number archive) to **The Best Estimators LLC** in the copy. Raw `phone_leads` inventory and global `phone_suppressions` remain platform-wide. Unknown user-scoped phone tables or already-tenantized source tables abort publication. A local nine-source dry-run succeeded before the later key rebuild; the local `source_scout.db` was absent, so this is not a test-server reconciliation. The copy remains schema version 1 and cannot enable tenant mode.
- Tenant mode now rejects public signup and unverified password reset, keeps the login wall on, and requires an existing platform admin whose factory password was rotated. Version-1 unified copies are rejected before router import or worker startup; no version-2 isolation migration exists yet.
- The unused legacy SQLAlchemy routers are excluded when tenant mode is enabled. Modern job, phone, research, campaign, and account routes still need explicit tenant-scoped reads/writes and ownership tests.
- Phone claim serving, the current call sheet, and daily claim counts accept an explicit tenant scope; the API rechecks live membership for search/leads/stats and rejects missing or unauthorized selectors. Claim selection is globally exclusive by normalized phone number, including when the raw pool has multiple business rows for that number; state capacity counts distinct servable numbers. The version-1 startup gate still prevents tenant-mode deployment.
- Dial/copy, non-terminal outcomes, saved contacts/notes, lead conversion, voicemail, wrong-number handling, and the wrong-number archive now use tenant-scoped ownership and reads/writes. Admin phone reports, per-user limits, and archive recovery require an active tenant where the admin has an owner/admin membership; guessed IDs from another tenant are rejected. The existing global raw pool and number exclusivity remain unchanged. Atomicity of terminal actions across their multiple SQLite writes still needs a concurrency/failure audit before a tenant-mode cutover.
- New offline copies rebuild all six private phone tables without a tenant default. Saved contacts use `UNIQUE(tenant_id, user_id, phone, kind)`, daily limits use `PRIMARY KEY(tenant_id, user_id)`, and ownership uses `UNIQUE(tenant_id, lead_id, user_id)`. Legacy rows, IDs, autoincrement sequences, and known call/claim indexes are preserved; unknown columns/custom indexes or triggers abort publication. On a version-1 unified copy in legacy mode, phone writes resolve the sole tenant explicitly and reject an ambiguous multi-tenant copy. Tenant-aware limit overrides and atomic serve quotas use the selected tenant. The version-2 startup gate remains closed. The auth/phones/database regression suites pass locally (243 tests); this is not a full-suite or server validation.
- Lead conversion, voicemail release/retirement, wrong-number retirement, and ordinary call events now recheck ownership inside one write transaction; a failed retirement rolls back its saved snapshot/event, and two simultaneous lead actions cannot both complete. Lead and final-voicemail retirement remove every raw business row sharing the phone number. Phone searches and calling actions now tag the selected tenant in the user activity audit. The remaining phone gate is admin role/entitlement review; broader two-tenant concurrency still needs validation. Other modern leads, research, Gmail, campaign, admin, cache, export, and worker paths still require a route-by-route tenant audit. Do not promote the unified marker to version 2 until those checks, migration reconciliation, backup restore, load, and rollback validation pass.
- LinkedIn raw person-profile inventory remains platform-wide, while the offline copy now maps its owner rows to the first tenant and rebuilds their uniqueness key with an explicit, non-default tenant ID. Serving is globally exclusive even when the same user belongs to two tenants, and uses an immediate transaction to prevent simultaneous cross-tenant claims. LinkedIn search/list/stats recheck live membership and the personal lead list is tenant-scoped. Search activity is tenant-tagged; its research-to-inventory byproduct path still needs the broader research/worker audit. This slice alone does not pass the tenant-mode deployment gate.
- Gmail account credentials are now mapped to the first tenant in an offline copy and rebuilt with an explicit `(tenant_id, user_id, provider, email)` key, preserving encrypted tokens and source files. Direct account reads, writes, refresh, status, and deletion take tenant scope; missing scope fails closed in tenant mode. Google OAuth state now binds user, tenant, expiry, and a persisted one-use nonce; callback checks current membership before and after Google's code exchange. Account and Gmail inbox/attachment/send/export endpoints recheck membership and use the selected tenant. Campaign API and scheduler sends/replies are not yet scoped, so this does not open the deployment gate.
- The frontend now fetches current memberships after login, validates its saved tenant against that list, blocks the workspace when no membership exists, sends the selected tenant header on requests, and includes the tenant in the Gmail OAuth navigation. A workspace switch clears cached query data and reloads before showing another workspace. This is UI plumbing only; backend authorization remains the security boundary.
- An offline copy now assigns existing campaign owner rows to the first tenant and removes the temporary tenant default; source files stay unchanged. Campaign create/list/detail/edit/delete and test-send API paths check current membership and use tenant-scoped Gmail accounts. The campaign scheduler now refuses to start or run a pass in tenant mode, and open tracking returns its pixel without an unscoped write. Campaign child tables, scoped tracking/replies, scheduler sends, CRM side effects, and worker ownership still need full tenant isolation. Do not promote the schema marker yet.
- The user activity log now stores an optional tenant ID. Offline copies assign historic rows to the first tenant; new phone and LinkedIn actions tag the selected tenant; Admin Activity requires live owner/admin membership and only lists rows of that tenant. Login/logout and platform-operation events have no selected tenant and are therefore absent from a tenant-filtered activity view; their separate platform-audit presentation is still a product/operations follow-up. Leads/CRM and other private activity producers must be tagged as their underlying routes become tenant-safe.
- The test server still uses its ten original operational SQLite files. Its separate legacy SQLAlchemy `leadhunter.db` is empty, but legacy routes remain in the API and its `companies`/`campaigns` names collide with active schemas.
- **Deployment gate:** Do not set `LEADHUNTER_UNIFIED_DB_PATH` or `LEADHUNTER_MULTI_TENANT_ENABLED` on the server until every private route, store query, export, OAuth callback, and worker carries and enforces tenant context; both-tenant isolation tests, live data reconciliation, backup restore, load, and rollback checks must pass. The startup preflight intentionally refuses version-1 copies.
- Phone allocation policy is confirmed: global exclusivity stays in force across tenants. Tenant creation/onboarding policy is pending confirmation; the safer proposed default is platform-admin creation plus invitations.

### Route isolation inventory (not an exit-gate pass)

The modern API currently declares 103 routes across the nine modules below. This is a code inventory, not a security certification: route-level dependencies alone do not prove that the underlying store, export, callback, and worker remain tenant-scoped.

| Module | Routes | Current tenant-mode assessment |
| --- | ---: | --- |
| `auth.py` | 7 | Signup/reset disabled; membership listing checks the live store. Login/logout are platform-scope events, not tenant-filtered activity. |
| `phones.py` | 13 | User operations have explicit tenant selection and live membership checks; remaining role/entitlement and broader concurrency review. |
| `admin.py` | 33 | Tenant/member, phone-limit/report, and selected-tenant activity reads are scoped or platform-admin-only; legacy lead, cache, account, key, Gmail, and lane operations need classification and tests. |
| `leads.py` | 22 | Jobs, dossiers, CRM, folders, deletes, CSV export, and background results still use user IDs without tenant isolation. Blocker. |
| `campaigns.py` | 11 | User-facing campaign CRUD and test-send check tenant membership/account ownership; child tables, tracking, reply handling, and scheduler/CRM side effects remain blockers. |
| `gmail_inbox.py` | 7 | Message reads, writes, attachment, and address export use tenant-scoped account access; campaign and frontend integration remain blockers. |
| `email_accounts.py` | 6 | Account endpoints and OAuth callback use tenant scope and live membership; frontend OAuth tenant selection and all consuming Gmail/campaign paths remain blockers. |
| `linkedin.py` | 3 | Search/results/stats, owner rows, and search activity are tenant-scoped; research-to-inventory provenance remains pending. |
| `source_intelligence.py` | 1 | Public, static source-planner catalog; it does not read customer records. Its unused legacy SQLAlchemy session dependency is a cleanup candidate, not a tenant-data path. |

The version-1 startup block remains mandatory while any blocker row is open. Never infer isolation from `user_id` alone: a user may belong to two tenants.

## 1. Decision in one minute

Keep one modular application initially. Add an explicit **tenant (customer organization)** to every private request, record, job, cache entry, export, and audit event. Keep official-board discovery and verified raw inventory as a **platform-wide supply layer**. Each tenant has its own users, claimed leads, call outcomes, Gmail connections, campaigns, settings, and usage. Move transactional state from the current SQLite files to a migration-controlled PostgreSQL design, with application authorization plus row-level security (RLS) as defense in depth. Separate API serving from durable background work before running multiple API instances.

This is a target architecture, not a claim that PostgreSQL, RLS, or a job queue already exists. The older `docs/architecture/database.md` describes PostgreSQL as a design, while current runtime stores such as `backend/app/auth/models.py`, `backend/app/phones/store.py`, and `backend/app/harvester/store.py` use SQLite.

## 2. Plain-language model

- **Platform:** LeadHunter Pro operates board discovery, ingestion, verification, and shared source inventory once for everyone.
- **Tenant:** One paying customer/company. It owns a private workspace and has its own policies and members.
- **User:** A person who belongs to one or more tenants. Their personal calling activity stays attributed to them inside the tenant.
- **Lead allocation:** A verified shared record becomes a tenant-visible lead only through an explicit allocation/claim. Sharing the raw source does not share another customer's notes or outcomes.

```text
Official boards / public sources
           |
           v
Platform source scout -> evidence gate -> shared raw inventory
                                             |
                                    eligibility/allocation
                                             |
                       +---------------------+---------------------+
                       |                                           |
                Tenant A workspace                           Tenant B workspace
             members, claims, calls                       members, claims, calls
             research, Gmail, campaigns                   research, Gmail, campaigns
```

## 3. What exists today, and what must change

| Current behavior (repository evidence) | Multi-tenant implication |
| --- | --- |
| `auth/models.py` stores users and an `is_admin` flag; `auth/dependencies.py` resolves a user from JWT or a shared anonymous account when authentication is off. | Add tenant memberships and separate platform/tenant roles. Multi-tenant mode must never use anonymous shared-account fallback. Recheck membership on each request. |
| `phones/store.py` keeps one global phone pool, user claims, personal outcomes, limits, and wrong-number archive. | Keep source supply global, but put tenant and user on allocations, outcomes, limits, and archives. Define whether a number is exclusive globally or only within a tenant. |
| `lead_research/service.py` shares dossiers through owner records and user-scoped views. | Split canonical public evidence from tenant-specific lead state, CRM, notes, decisions, and suppression. |
| `email_accounts/store.py` stores encrypted Gmail tokens against a user; `campaigns/store.py` stores user-owned campaigns. | Bind OAuth state, credentials, campaigns, sends, replies, and webhooks to both tenant and authorized user/account. |
| `harvester/store.py` and `source_scout/store.py` hold global demand, cursors, source schedules, and snapshots. | Platform-owned, not copied per tenant; demand may be aggregated from tenants without disclosing their private queries. |
| `main.py` starts campaign, enrichment, harvester, and scout threads inside the API process. | Multiple API replicas would risk duplicate work. Move to durable queue plus leased/single-owner schedules before horizontal scaling. |
| `api/v1/router.py` includes old and new routes, with mixed authorization patterns. | Inventory every route, export, callback, and admin operation; enforce tenant scope server-side before public onboarding. |

## 4. Target components and ownership

| Component | Owner/scope | Required boundary |
| --- | --- | --- |
| Identity and tenant registry | Platform | `users`, `tenants`, `memberships`, invitations, session/revocation state. |
| Source scout and official board adapters | Platform | One scheduled discovery/ingestion lane with verified provenance, cursor, retries, and source policy. |
| Canonical public inventory | Platform | Deduplicated source records and evidence; no customer notes, call outcomes, or OAuth data. |
| Allocation and entitlement service | Tenant-aware | Validates tenant plan, state/trade eligibility, global exclusivity policy, and atomic claim. |
| Lead and phone workspace | Tenant; user attribution | Claims, sheet, calls, CRM, notes, archives, quotas, and exports scoped to tenant. |
| Research and AI jobs | Tenant execution; optionally shared public evidence | Tenant context carried through job, prompt, cache, evidence, and result; no private cross-tenant reuse. |
| Gmail and campaigns | Tenant; account owner | Encrypted credentials, send authority, tracking, reply detection, and rate limits bound to tenant. |
| Audit and operations | Platform metadata; tenant-visible subset | Immutable actor, tenant, action, target, time, and outcome; support access separately approved and logged. |

Do not introduce microservices in the first migration. Keep module boundaries while extracting worker processes and persistence adapters only where migration requires them.

## 5. Identity, authorization, and UX contract

1. A user may join multiple tenants through a membership. An active tenant is selected explicitly. A request-supplied ID, URL slug, or header is only a selector, never proof of authorization.
2. Verify the user session and current membership against the server-side store on every protected request. Tenant changes issue a new scoped session or are revalidated per request. Revocation and role changes must take effect without waiting for an old JWT to expire.
3. Roles: `platform_admin` for national sources/operations; `tenant_owner` for organization ownership; `tenant_admin` for members and limits; `member` for assigned work. Phones/emails access is a separate entitlement, not an admin role.
4. Platform administration must not silently expose tenant-private Gmail, notes, or call records. Exceptional support access needs a reason, expiry, and audit trail.
5. UI shows active tenant, switcher, members/invitations, role/entitlement management, tenant usage, and tenant-specific reports. Personal sheet and calling history still attribute actions to the user.
6. Disable auth-off/shared-account mode for multi-tenant operation. Replace fixed first-boot admin credentials with a controlled bootstrap and rotate secrets before onboarding customers.

## 6. Data architecture and enforcement

**Recommended target:** PostgreSQL for mutable transactional data, with separate platform and tenant domains. This is a migration project, not a connection-string change. Existing SQL, store constructors, in-place `CREATE TABLE`/`ALTER` paths, and test fixtures must be inventoried and converted deliberately.

**Core entities (logical, not final DDL):** `tenants`, `users`, `memberships`, `tenant_entitlements`, `tenant_usage_ledger`, `source_records`, `source_evidence`, `tenant_lead_allocations`, `tenant_leads`, `phone_call_events`, `tenant_phone_limits`, `wrong_number_archive`, `research_jobs`, `research_results`, `email_accounts`, `campaigns`, `campaign_sends`, `audit_events`.

- Every private row carries `tenant_id`; user-attributed rows also carry `user_id`. Foreign keys and unique constraints include tenant identity where the resource is tenant-owned. Never infer tenant from a lead ID, email address, or phone number alone.
- The API resolves an authenticated `TenantContext` once and passes it to stores/services. Every read, write, count, export, and delete uses it. Missing context fails closed.
- RLS on tenant tables is a second boundary: transaction-local tenant context, `USING` and `WITH CHECK` policies, runtime role without `BYPASSRLS` or table-owner bypass. Connection pools must reset context between requests. Platform inventory policies differ from private tables. Application authorization remains mandatory. See [PostgreSQL's RLS documentation](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).
- Cache keys, files, generated CSVs, AI prompts, telemetry, and backups follow the same classification. Tenant-specific cache results cannot be reused across customers unless proven public-only.
- Define retention and deletion separately for shared official-source records, tenant claims, personal activity, wrong-number archive, OAuth credentials, and audit evidence. A customer's deletion must not delete another customer's lawful independent record.

## 7. Allocation, quotas, and source policy

**Default migration rule:** Preserve today's global exclusivity for phone claims and researched lead ownership until the business explicitly changes it. The meeting must decide whether two independent tenants may receive the same public business number later. Do not let a database uniqueness constraint decide this accidentally.

Quota accounting should use durable claim/usage events and atomic transactions, not a counter shown only in UI. Define tenant-level plan allowance and optional per-user daily allowance; admin overrides cannot exceed a tenant's purchased/approved entitlement without platform authorization. UTC versus tenant-local reset time is a product decision; current phone sheet uses UTC and should keep that during the first migration.

Boards and AI source discovery continue daily at platform level. A candidate source must pass provenance, public-access, stable download, phone/trade/business quality, deduplication, and monitoring gates before promotion. A source failure cannot halt the whole discovery loop. No inferred trade or invented person is allowed.

## 8. Background processing and integrations

- Put tenant jobs on durable queues with `tenant_id`, actor, idempotency key, attempt count, lease, and result reference. Retry only safe/idempotent steps; send-email side effects require duplicate-send prevention.
- Run platform scout/harvester under a platform schedule with a lease; run tenant research, enrichment, and campaign work under tenant context. API startup must not silently start a second copy when replicas increase.
- Bind Google OAuth `state` to tenant, user, expiry, and nonce; revalidate membership at callback. Store credentials encrypted, restrict their use to the owning tenant/account, and make disconnect/revocation immediate.
- Correlate provider requests, job runs, and audit events with tenant-safe identifiers. Logs must not print tokens or another tenant's private payload.

## 9. Migration and rollout sequence

| Phase | Deliverable | Exit gate before next phase |
| --- | --- | --- |
| 0. Inventory and decisions | Classify every table, endpoint, worker, cache, export, secret, and backup; decide claim exclusivity and tenant roles. | No unclassified path; security and product decisions recorded. |
| 1. Tenant identity | Tenant/membership model, role matrix, scoped request context, auth hardening, legacy-route audit. | Tenant A cannot invoke Tenant B operations; revoked membership fails immediately. |
| 2. Storage foundation | PostgreSQL migrations and persistence adapters; RLS and indexes; migration/reconciliation tooling. | Copied data counts/checksums match; missing tenant context denied; backup restore proven. |
| 3. Leads and phones | Shared supply, atomic tenant claims, call sheets, outcomes, quotas, archives, research/CRM split. | No cross-tenant rows or counts; existing one-tenant behavior preserved. |
| 4. Gmail and campaigns | Tenant-bound OAuth/accounts, campaigns, send/reply/tracking flows. | Cross-tenant send impossible; duplicate send prevented; revoke works. |
| 5. Workers and AI | Durable tenant jobs, platform leases, idempotency, source discovery monitoring. | Restart and two-replica tests show no duplicate work or lost jobs. |
| 6. UI and staged release | Tenant switcher/admin/usage UI; migrated first tenant; canary and operations runbooks. | Test-server isolation, load, restore, and rollback gates pass before production cutover. |

For the first existing installation, create one designated legacy tenant and map existing users, user-owned rows, and service-owned inventory to it or to the platform domain according to the inventory. Preserve original IDs and provenance in a migration map. Test migration on a snapshot; at cutover pause writes, take a consistent backup, import, compare counts/hashes and representative records, then switch traffic. Keep the old system read-only for rollback. Never overwrite current server SQLite files as a shortcut.

## 10. Minimum security and acceptance suite

- For each private route and store operation: Tenant A creates a resource; Tenant B cannot read, count, update, delete, export, or infer it; Tenant A can. Include guessed IDs, pagination, filters, and bulk exports.
- Role/entitlement tests: member, tenant admin, platform admin, revoked member, user in two tenants, token-less visitor.
- RLS tests with ordinary runtime DB role, missing tenant context, wrong tenant context, pooled-connection reuse, and background jobs.
- Concurrency tests: simultaneous claims respect exclusivity and quota; retries do not duplicate claims, AI jobs, or sends.
- Migration tests on copies of every SQLite database: count/hash reconciliation, legacy ownership, rollback/restore, and no lost call history or OAuth account.
- Operational gates: per-tenant metrics, error rate, queue depth, CPU/memory, source freshness, backup restore, and sustained test-server smoke before production.

## 11. Decisions to obtain in the meeting

1. Is a **tenant** one company/team, and may one person join multiple companies?
2. May the same public phone number/lead be sold or served to two tenants, or must exclusivity remain global?
3. Which data may teammates share, and which must remain personal (especially calls, Gmail, and notes)?
4. What are the plan limits: users, daily numbers, AI/research jobs, Gmail accounts, sending, and storage? Who may override them?
5. Should daily limits reset in UTC (current behavior) or each tenant's local time zone?
6. Is a tenant allowed to bring a private board/source, and may that source ever enter the shared inventory?
7. What retention/deletion and support-access rules do customers expect?
8. What is the acceptable maintenance window and rollback requirement for the eventual cutover?

## 12. Explicit non-goals and risks

This plan does not authorize implementation, production deployment, paid-board acquisition, customer-data sharing, or changing phone exclusivity. Main risks are an overlooked legacy route/query, missing tenant context in a worker, leaked cache/export, misassigned legacy row, duplicate campaign send, and premature multi-replica startup. The phased gates above exist to expose these before customer onboarding.
