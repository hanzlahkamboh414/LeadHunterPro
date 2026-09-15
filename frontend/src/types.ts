// Shared API types mirroring the backend schemas (app/schemas/leads.py +
// app/lead_research/models.py). Keep in sync with the backend by hand.

export type JobState =
  | "queued"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface JobEvent {
  phase: string;
  step: number;
  total: number;
  message: string;
  email: string;
  data: Record<string, unknown> | null;
  ts: string;
}

export interface LeadResult {
  email: string;
  domain: string;
  company: string;
  person: string;
  bound: boolean;
  score: number;
  recommendation: string;
  intent: string;
  timing: string;
  error?: string;
}

export interface JobQuery {
  trade: string;
  location: string;
  target_emails: number;
  discover_only?: boolean;
  /** OPTIONAL label for this run, stored as a tag on every lead it produces. */
  search_name?: string;
  /** OPTIONAL folder to auto-file every lead this run produces into. */
  folder?: string;
}

export interface Job {
  id: string;
  query: JobQuery;
  state: JobState;
  events: JobEvent[];
  results: LeadResult[];
  pass_log: Array<Record<string, unknown>>;
  error: string | null;
  created_at: string;
  updated_at: string;
  elapsed_s: number;
  /** Run outcome (H1): honest delivery vs target. */
  working_leads?: number;
  leads_found?: number;
  shortfall?: number;
  shortfall_reason?: string;
}

export interface JobSummary {
  id: string;
  query: JobQuery;
  state: JobState;
  error: string | null;
  created_at: string;
  updated_at: string;
  elapsed_s: number;
  working_leads?: number;
  leads_found?: number;
  shortfall?: number;
  shortfall_reason?: string;
}

export interface LeadSummary {
  email: string;
  domain: string;
  company: string;
  person: string;
  role: string;
  bound: boolean;
  linkedin: string;
  /** Future-ready — the research pipeline does not collect phones yet, so this
   * is empty today. Kept in the contract so a later step can fill it in. */
  phone: string;
  score: number;
  recommendation: string;
  intent: string;
  reason: string;
  timing: string;
  /** Which query run produced this lead, e.g. "General Contractors · Dallas TX". */
  source: string;
  /** Extraction date (YYYY-MM-DD) — the day this lead was first researched
   * ("kis tareekh ko nikala"), for date filtering/grouping. */
  created_at: string;
  /** User organization metadata (Phase B): one primary folder (exclusive) +
   * free-form multi-tags. Lives outside the research payload — never clobbered
   * by a pipeline re-research. */
  folder: string;
  tags: string[];
  /** CRM pipeline stage (Phase E1) — "researched" is the honest default: a
   * stored dossier has by definition been through research. */
  crm_status: string;
  /** The user's next step on this lead (free text, empty = none). */
  next_action: string;
}

/** One catalog folder — a persisted, clickable group (empty allowed). */
export interface FolderItem {
  name: string;
  created_at: string;
  count: number;
}

/**
 * The organization mailbox overview (GET /leads/folders). `folders` are the
 * persisted groups; `unfiled` is what the DEFAULT Companies view shows (leads
 * still in the inbox); `total` counts every dossier (folders included) so the
 * "All" chip + Dashboard totals stay honest.
 */
export interface FoldersOut {
  folders: FolderItem[];
  unfiled: number;
  total: number;
}

/** POST /leads/folders response — the row + whether it was newly created. */
export interface FolderCreateOut {
  name: string;
  created_at: string;
  count: number;
  created: boolean;
}

export interface EvidenceFact {
  claim: string;
  source_url: string;
  source_type: string;
  source_note?: string;
  confidence: string;
}

export interface LeadDetail {
  email: string;
  domain: string;
  refined_domain: string;
  refined_company: string;
  company: {
    name: string;
    industry: string;
    location: string;
    website: string;
    facts: EvidenceFact[];
  };
  person: {
    name: string;
    role: string;
    role_relevance: boolean;
    bound: boolean;
    linkedin: string;
    evidence: EvidenceFact[];
  };
  intent: {
    needs_estimation: string;
    signal: string;
    reason: string;
    evidence: EvidenceFact[];
  };
  timing: {
    window: string;
    reason: string;
    events: EvidenceFact[];
  };
  fit: string;
  potential_score: number;
  recommendation: string;
  sources_checked: string[];
  source_errors: Record<string, string>;
  /** Extraction date (YYYY-MM-DD). */
  created_at: string;
  folder: string;
  tags: string[];
  /** CRM pipeline state (Phase E1). */
  crm_status: string;
  next_action: string;
}

/** The CRM pipeline stages, in journey order (Phase E1). Must match
 * backend `app.lead_research.models.CRM_STATUSES` exactly. */
export const CRM_STAGES = [
  "new",
  "researched",
  "qualified",
  "contacted",
  "opened",
  "replied",
  "interested",
  "meeting",
  "won",
  "lost",
] as const;

export type CrmStage = (typeof CRM_STAGES)[number];

/** One immutable timeline event on a lead (stage change / next action /
 * note — email sends append here in Phase E3+). */
export interface CrmEvent {
  id: number;
  kind: string;
  detail: string;
  user_id: string;
  username: string;
  created_at: string;
}

/** A lead's full CRM state (GET /leads/{email}/crm). */
export interface CrmState {
  crm_status: string;
  next_action: string;
  events: CrmEvent[];
}

/** PUT /leads/{email}/crm body — only the fields you send are touched. */
export interface CrmInput {
  status?: string;
  next_action?: string;
  note?: string;
}

/** One campaign (Phase E3) — an email outreach run on a connected Gmail. */
export interface Campaign {
  id: number;
  account_id: number;
  /** All sending accounts, primary first (Phase E5). */
  account_ids: number[];
  name: string;
  subject: string;
  body: string;
  status: string; // scheduled | running | paused | completed
  paused_reason: string; // user | account | rate_limited
  resume_at: string;
  start_at: string;
  daily_limit: number;
  delay_min_s: number;
  delay_max_s: number;
  /** AI opening line on first emails, from verified evidence only (E5). */
  ai_personalize: boolean;
  created_at: string;
  updated_at: string;
  pending: number;
  sent: number;
  failed: number;
  /** Follow-ups dropped because the lead replied (Phase E4). */
  skipped: number;
  /** Leads who answered this campaign (Phase E4). */
  replied: number;
  account_email: string;
  /** Resolved addresses of account_ids, primary first (E5). */
  account_emails: string[];
}

/** One rung of a campaign's follow-up ladder (Phase E4). */
export interface CampaignFollowup {
  step: number;
  after_days: number;
  subject: string;
  body: string;
}

/** One row of a campaign's send queue. */
export interface CampaignSend {
  id: number;
  email: string;
  /** 0 = the original email; 1+ = follow-up rungs (Phase E4). */
  step: number;
  state: string; // pending | sent | failed | skipped
  subject: string;
  sent_at: string;
  /** Earliest send time (follow-ups: previous send + after_days). */
  not_before: string;
  attempts: number;
  error: string;
  /** Which account sent this row (0 = pending / pre-E5). */
  account_id: number;
  /** First open time from the tracking pixel ('' = never opened). */
  opened_at: string;
  /** Total opens recorded (image loads — a signal, not a proof). */
  opened_count: number;
  /** When this lead's reply arrived ('' = no reply yet). */
  replied_at: string;
}

/** PUT /campaigns/{id} body — edit the pitch of a started campaign. */
export interface CampaignUpdateInput {
  name: string;
  subject: string;
  body: string;
}

/** One risky thing the spam analyzer found, in plain words. */
export interface SpamFinding {
  rule: string;
  severity: string; // high | medium | low
  message: string;
  count: number;
  /** Concrete rewrite advice (AI findings carry this). */
  fix: string;
  /** Which judgment category this belongs to (AI findings). */
  category: string;
}

/** POST /campaigns/spam-check result — blended AI + rules spam risk. */
export interface SpamCheckResult {
  score: number; // 0-100
  level: string; // low | medium | high
  findings: SpamFinding[];
  /** One short plain-words sentence from the AI (empty when rules-only). */
  summary: string;
  method: string; // ai | rules
  /** AI's per-category risk breakdown (0-100 each; empty when rules-only). */
  categories: Record<string, number>;
}

/** POST /campaigns/spam-improve result — the one-click fixed pitch. */
export interface SpamImproveResult {
  subject: string;
  body: string;
  method: string; // ai | rules
  notes: string[];
}

/** A follow-up rung in a POST /campaigns body (Phase E4). */
export interface FollowupInput {
  after_days: number;
  subject: string;
  body: string;
}

/** POST /campaigns body. */
export interface CampaignCreateInput {
  name: string;
  account_id: number;
  /** Extra sending accounts beyond the primary, max 4 (Phase E5). The
   * scheduler spreads sends across all of them. */
  account_ids?: number[];
  subject: string;
  body: string;
  emails: string[];
  /** ISO datetime with offset — the 9:00 AM start, in the user's timezone. */
  start_at: string;
  daily_limit?: number;
  delay_min_s?: number;
  delay_max_s?: number;
  /** Optional follow-up emails (max 3), each cancelled if the lead replies. */
  followups?: FollowupInput[];
  /** Prepend an AI opening line (verified evidence only) to first emails. */
  ai_personalize?: boolean;
}

/** A connected sending account (Phase E2) — Gmail via Google OAuth. This is
 * the SAFE public view: OAuth tokens never leave the backend. */
export interface EmailAccount {
  id: number;
  provider: string;
  email: string;
  display_name: string;
  /** connected | expired | revoked (revoked = reconnect needed). */
  status: string;
  /** Space-separated OAuth scopes this account actually granted. Accounts
   * connected before Phase E4 lack gmail.readonly → reply detection is off
   * for them until they reconnect. Phase E7 added gmail.modify (star /
   * mark-read / trash) — same one-reconnect rule. */
  scopes: string;
  created_at: string;
}

// Gmail inbox (Phase E7) — the connected account's mail inside the app.

/** One row of the message list (metadata only; bodies load on open). */
export interface GmailMessageRow {
  id: string;
  thread_id: string;
  from: string;
  to: string;
  subject: string;
  date: string;
  snippet: string;
  unread: boolean;
  starred: boolean;
}

export interface GmailMessagePage {
  messages: GmailMessageRow[];
  next_page_token: string;
  total_estimate: number;
}

export interface GmailAttachment {
  attachment_id: string;
  filename: string;
  mime_type: string;
  size: number;
}

/** One full message — the reading pane. */
export interface GmailMessage {
  id: string;
  thread_id: string;
  snippet: string;
  headers: {
    from: string;
    to: string;
    cc: string;
    subject: string;
    date: string;
    "message-id": string;
    "in-reply-to": string;
    references: string;
  };
  labels: string[];
  text: string;
  html: string;
  attachments: GmailAttachment[];
  unread: boolean;
  starred: boolean;
}

export interface GmailSendInput {
  account_id: number;
  to: string;
  cc?: string;
  bcc?: string;
  subject: string;
  body: string;
  in_reply_to?: string;
  references?: string;
}

export interface GmailModifyInput {
  account_id: number;
  add_labels: string[];
  remove_labels: string[];
}

/** The address-export filters — source plus an optional date window. */
export interface GmailExportFilter {
  account_id: number;
  source: "sent" | "received";
  year?: number;
  from_date?: string;
  to_date?: string;
}

export interface AdminJobSummary {
  id: string;
  query: JobQuery;
  state: JobState;
  error: string;
  created_at: string;
  updated_at: string;
  elapsed_s: number;
}

export interface AdminDashboard {
  generated_at: string;
  database_path: string;
  dossiers_total: number;
  recommendations: Record<string, number>;
  pending: Record<string, number>;
  jobs: Record<string, number>;
  risks: Record<string, number>;
  recent_jobs: AdminJobSummary[];
}

/** Admin — API key status. Only a masked tail is ever returned (never full). */
export interface AdminKey {
  name: string;
  configured: boolean;
  masked: string;
}

export interface AdminKeys {
  keys: AdminKey[];
  provider: string;
  base_url: string;
  model: string;
  searxng_url: string;
  env: string;
  overlay_path: string;
  applies_after_restart: boolean;
}

/** Admin — deleted-lead audit trail ("kon kon c email delete ki"). */
export interface AdminDeletedRow {
  email: string;
  deleted_at: string;
  reason: string;
  user_id: string;
  username: string;
  /** '' pending the admin's answer / 'confirmed' / 'restored'. */
  admin_decision: string;
}

export interface AdminDeleted {
  total: number;
  deleted: AdminDeletedRow[];
}

/** Admin's Confirm/Restore answer on one deleted lead. */
export interface AdminDecision {
  email: string;
  admin_decision: string;
  /** 'yes' when a stashed dossier was re-saved (restore), else 'no'/''. */
  restored_dossier: string;
}

/** Admin — the discovery cache (pending_leads) viewer. */
export interface AdminPendingRow {
  email: string;
  location: string;
  dead: boolean;
  attempted_at: string;
  attempt_count: number;
}

export interface AdminCachePending {
  total: number;
  active: number;
  dead: number;
  rows: AdminPendingRow[];
}

/** Admin — the provider-neutral search-result cache status. */
export interface AdminSearchCache {
  path: string;
  search_rows: number;
  extract_rows: number;
  search_ttl_days: number;
  extract_ttl_days: number;
  hit_rate: number;
  top_queries: Array<{ query: string; max_results: number; fetched_at: string }>;
}

/** Admin — Dashboard data control (hide / show / delete/assign user-facing leads). */
export type AdminLeadScopeKind = "date" | "source" | "email" | "folder";

export interface AdminLeadScope {
  scope: AdminLeadScopeKind;
  value: string;
}

export interface AdminLeadAction {
  affected: number;
  emails: string[];
}

export interface AdminVisibilityDate {
  date: string;
  total: number;
  hidden: number;
}

export interface AdminVisibility {
  total: number;
  hidden: number;
  by_date: AdminVisibilityDate[];
}

/** Admin — user management + activity log. */
export interface AdminUser {
  id: string;
  username: string;
  email: string;
  is_admin: boolean;
  created_at: string;
  /** Display name (topbar); empty -> UI falls back to username. */
  name: string;
}

export interface AdminUsers {
  total: number;
  users: AdminUser[];
}

export interface AdminActivityRow {
  id: number;
  user_id: string;
  username: string;
  action: string;
  detail: string;
  created_at: string;
}

export interface AdminActivity {
  total: number;
  activity: AdminActivityRow[];
}

export interface AdminUserLeadSummary {
  user_id: string;
  username: string;
  folders: Record<string, number>;
  unfiled: number;
  total: number;
  dates: string[];
}

// Phones vertical (P3) -------------------------------------------------------

/** One phone lead served from the shared pool (license-board sourced).
 *  `email` is enrichment output: only an address literally seen on the
 *  company's own website — never a guess. `email_status` is the honest
 *  lifecycle: "pending" (background enricher hasn't reached it), "found",
 *  or "none" (tried, nothing findable). */
export interface PhoneLead {
  id: number;
  phone: string;
  person_name: string;
  business_name: string;
  trade: string;
  city: string;
  state: string;
  source: string;
  license_status: string;
  source_url: string;
  email: string;
  email_source: string;
  website: string;
  email_status: "pending" | "found" | "none";
  /** Calling workflow (P7.5): voicemails this number has drawn so far. */
  voicemail_count: number;
}

/** A saved phone row (✓Lead or 💾Store output) — a full snapshot that
 *  survives the pool row's retirement, plus the caller's note. */
export interface PhoneSaved {
  id: number;
  phone: string;
  person_name: string;
  business_name: string;
  trade: string;
  city: string;
  state: string;
  source: string;
  source_url: string;
  license_status: string;
  email: string;
  email_source: string;
  website: string;
  kind: "lead" | "contact";
  note: string;
  created_at: string;
  updated_at: string;
}

/** POST /phones/search — the served leads + honest harvest telemetry. */
export interface PhoneSearchResult {
  leads: PhoneLead[];
  served_from_pool: number;
  fetched_live: number;
  stocked_new: number;
  banked_other_trade: number;
  dropped_bad_phone: number;
  coverage: string[];
  reason: string;
}

/** GET /phones/stats — the shared pool's honest inventory. */
export interface PhonePoolStats {
  total: number;
  claimed: number;
  unclaimed: number;
  by_trade: Record<string, number>;
  by_state: Record<string, number>;
  mine: number;
}

/** Signup category — which verticals the account uses. */
export type SignupCategory = "emails" | "phones" | "both";

// LinkedIn vertical (P4) -----------------------------------------------------

/** One LinkedIn person lead — an email-research byproduct (no live fetch,
 *  no quota): the decision-maker's profile URL found during research. */
export interface LinkedInLead {
  id: number;
  person_name: string;
  role: string;
  linkedin_url: string;
  company_name: string;
  domain: string;
  trade: string;
  city: string;
  state: string;
  source: string;
  source_email: string;
}

/** POST /linkedin/search — pool-only serve with an honest shortfall reason. */
export interface LinkedInSearchResult {
  leads: LinkedInLead[];
  served_from_pool: number;
  reason: string;
}
