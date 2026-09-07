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
}

export interface JobSummary {
  id: string;
  query: JobQuery;
  state: JobState;
  error: string | null;
  created_at: string;
  updated_at: string;
  elapsed_s: number;
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
}

export interface AdminDeleted {
  total: number;
  deleted: AdminDeletedRow[];
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
