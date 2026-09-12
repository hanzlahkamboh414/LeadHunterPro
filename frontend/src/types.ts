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
