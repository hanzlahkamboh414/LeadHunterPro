// Thin fetch client for the Leads API. All calls go through the Vite dev
// proxy at /api (see vite.config.ts) so there's no CORS in dev.
//
// Auth: the backend has an optional LEADS_API_KEY baseline (M12). If a key is
// configured in the UI (persisted to localStorage) it is sent as X-API-Key.

import type {
  AdminActivity,
  AdminCachePending,
  AdminDashboard,
  AdminDeleted,
  AdminKeys,
  AdminLeadAction,
  AdminLeadScope,
  AdminSearchCache,
  AdminUserLeadSummary,
  AdminUsers,
  AdminVisibility,
  FolderCreateOut,
  FoldersOut,
  Job,
  JobQuery,
  JobSummary,
  LeadDetail,
  LeadSummary,
} from "../types";

const BASE = import.meta.env.VITE_API_BASE || "/api/v1";

const KEY_STORAGE = "leadhunter.api_key";

export function getStoredApiKey(): string {
  try {
    return localStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

export function setStoredApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* ignore private-mode failures */
  }
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** Auth header map (X-API-Key when the UI has configured one), merged over any
 * init headers on the caller's request. Also sends JWT Bearer token when logged
 * in (Phase 3 auth). */
function authHeaders(init?: RequestInit): Record<string, string> {
  const headers: Record<string, string> = {
    ...(init?.headers as Record<string, string> | undefined),
  };
  const key = getStoredApiKey();
  if (key) headers["X-API-Key"] = key;
  // JWT token from auth system
  try {
    const token = localStorage.getItem("leadhunter.jwt");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  } catch {
    /* ignore private-mode failures */
  }
  return headers;
}

/** Query-string for the leads list — shared by listLeads / pageLeads so the
 * Companies screen filters behave identically across both call shapes. */
function leadsParams(filter: LeadsFilter): string {
  const q = new URLSearchParams();
  if (filter.recommendation) q.set("recommendation", filter.recommendation);
  if (filter.bound !== undefined) q.set("bound", String(filter.bound));
  if (filter.min_score !== undefined) q.set("min_score", String(filter.min_score));
  if (filter.source) q.set("source", filter.source);
  if (filter.folder) q.set("folder", filter.folder);
  if (filter.tag) q.set("tag", filter.tag);
  if (filter.date) q.set("date", filter.date);
  if (filter.q) q.set("q", filter.q);
  if (filter.limit !== undefined) q.set("limit", String(filter.limit));
  if (filter.offset !== undefined) q.set("offset", String(filter.offset));
  return q.toString();
}

async function request<T>(path: string, init?: RequestInit, text = false): Promise<T> {
  const headers = authHeaders(init);

  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  if (text) return (await res.text()) as T;
  return (await res.json()) as T;
}

export interface CreateJobInput {
  trade: string;
  location: string;
  target_emails: number;
  discover_only?: boolean;
  /** OPTIONAL label for this run, stored as a tag on every lead it produces. */
  search_name?: string;
  /** OPTIONAL folder to auto-file every lead this run produces into. */
  folder?: string;
}

export interface LeadsFilter {
  recommendation?: string;
  bound?: boolean;
  min_score?: number;
  source?: string;
  folder?: string;
  tag?: string;
  date?: string;
  /** Identity text search (company / email / person / role) — matched server-side. */
  q?: string;
  limit?: number;
  offset?: number;
}

export interface OrganizeInput {
  folder: string;
  tags: string[];
}

/** CSV-export filter — the same user-view filters as the Companies list. */
export interface ExportFilter {
  recommendation?: string;
  emails?: string[];
  folder?: string;
  tag?: string;
  date?: string;
  source?: string;
  bound?: boolean;
  min_score?: number;
  q?: string;
}

export const api = {
  // Auth -----------------------------------------------------------------
  login(username: string, password: string): Promise<{ user_id: string; username: string; is_admin: boolean; token: string }> {
    return request("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
  },

  signup(username: string, email: string, password: string, name = ""): Promise<{ user_id: string; username: string; name: string; token: string }> {
    return request("/auth/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, email, password, name }),
    });
  },

  resetPassword(username: string, new_password: string): Promise<{ success: boolean }> {
    return request("/auth/reset-password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, new_password }),
    });
  },

  getMe(): Promise<{ user_id: string; username: string; email: string; is_admin: boolean }> {
    return request("/auth/me");
  },

  /** Record the logout in the admin activity log (fire-and-forget; the client
   * discards the token right after — the JWT itself is stateless). */
  logout(): Promise<{ success: boolean }> {
    return request("/auth/logout", { method: "POST" });
  },

  adminDashboard(): Promise<AdminDashboard> {
    return request<AdminDashboard>("/admin/dashboard");
  },

  // Admin — API key management (masked status only; the FULL value is never
  // returned, so the UI can only show configured/masked + set/clear).
  adminKeys(): Promise<AdminKeys> {
    return request<AdminKeys>("/admin/keys");
  },

  updateAdminKey(name: string, value: string): Promise<AdminKeys> {
    return request<AdminKeys>("/admin/keys", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, value }),
    });
  },

  /** Admin — which emails were deleted + when + why. */
  adminDeleted(limit = 200): Promise<AdminDeleted> {
    return request<AdminDeleted>(`/admin/deleted?limit=${limit}`);
  },

  /** Admin — the discovery cache (pending_leads) viewer. */
  adminPendingCache(limit = 100): Promise<AdminCachePending> {
    return request<AdminCachePending>(`/admin/cache/pending?limit=${limit}`);
  },

  /** Admin — provider-neutral search-cache status. */
  adminSearchCache(): Promise<AdminSearchCache> {
    return request<AdminSearchCache>("/admin/cache/search");
  },

  /** Admin — purge TTL-expired search-cache entries. */
  purgeSearchCache(): Promise<{ removed: number }> {
    return request("/admin/cache/purge-search", { method: "POST" });
  },

  // Admin — Dashboard data control (hide / show / delete user-facing leads).
  adminVisibility(): Promise<AdminVisibility> {
    return request<AdminVisibility>("/admin/leads/visibility");
  },
  adminHideLeads(body: AdminLeadScope): Promise<AdminLeadAction> {
    return request<AdminLeadAction>("/admin/leads/hide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  adminShowLeads(body: AdminLeadScope): Promise<AdminLeadAction> {
    return request<AdminLeadAction>("/admin/leads/show", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  adminDeleteLeads(body: AdminLeadScope): Promise<AdminLeadAction> {
    return request<AdminLeadAction>("/admin/leads/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  /** Admin — push a date/source/folder/email slice to a user's dashboard
   * (userId "" pulls it back to admin-only). */
  adminAssignLeads(body: AdminLeadScope & { user_id: string }): Promise<AdminLeadAction> {
    return request<AdminLeadAction>("/admin/leads/assign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  // Admin — user management + activity log.
  adminUsers(): Promise<AdminUsers> {
    return request<AdminUsers>("/admin/users");
  },
  adminCreateUser(body: {
    username: string;
    email: string;
    password: string;
    name?: string;
  }): Promise<AdminUsers["users"][number]> {
    return request("/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },
  adminDeleteUser(userId: string): Promise<{ success: boolean; username: string }> {
    return request(`/admin/users/${encodeURIComponent(userId)}`, {
      method: "DELETE",
    });
  },
  adminResetUserPassword(
    userId: string,
    newPassword: string
  ): Promise<{ success: boolean; username: string }> {
    return request(`/admin/users/${encodeURIComponent(userId)}/password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: newPassword }),
    });
  },
  adminActivity(limit = 200, userId = ""): Promise<AdminActivity> {
    const qs = userId ? `?limit=${limit}&user_id=${encodeURIComponent(userId)}` : `?limit=${limit}`;
    return request<AdminActivity>(`/admin/activity${qs}`);
  },
  adminUserLeadSummary(userId: string): Promise<AdminUserLeadSummary> {
    return request<AdminUserLeadSummary>(
      `/admin/users/${encodeURIComponent(userId)}/leads-summary`
    );
  },

  // Jobs ---------------------------------------------------------------
  createJob(body: CreateJobInput): Promise<Job> {
    return request<Job>("/leads/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body satisfies JobQuery),
    });
  },

  listJobs(): Promise<JobSummary[]> {
    return request<JobSummary[]>("/leads/jobs");
  },

  getJob(id: string): Promise<Job> {
    return request<Job>(`/leads/jobs/${encodeURIComponent(id)}`);
  },

  cancelJob(id: string): Promise<{ job_id: string; state: string }> {
    return request(`/leads/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    });
  },

  pauseJob(id: string): Promise<{ job_id: string; state: string }> {
    return request(`/leads/jobs/${encodeURIComponent(id)}/pause`, {
      method: "POST",
    });
  },

  resumeJob(id: string): Promise<{ job_id: string; state: string }> {
    return request(`/leads/jobs/${encodeURIComponent(id)}/resume`, {
      method: "POST",
    });
  },

  // Leads ---------------------------------------------------------------
  listLeads(filter: LeadsFilter = {}): Promise<LeadSummary[]> {
    const qs = leadsParams(filter);
    return request<LeadSummary[]>(`/leads${qs ? `?${qs}` : ""}`);
  },

  /** Paged leads for ONE page of the Companies screen.
   *
   * The backend filters, sorts and paginates IN SQL, so only a page is
   * downloaded (never the whole store — the root cause of the slow screen).
   * ``total`` is the honest count for the SAME filters (from the X-Total-Count
   * header) so the UI can page with "Show more" and still report true totals.
   */
  async pageLeads(
    filter: LeadsFilter = {},
  ): Promise<{ rows: LeadSummary[]; total: number }> {
    const qs = leadsParams(filter);
    const res = await fetch(`${BASE}/leads${qs ? `?${qs}` : ""}`, {
      headers: authHeaders(),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        if (typeof body.detail === "string") detail = body.detail;
      } catch { /* non-JSON error body */ }
      throw new ApiError(res.status, detail);
    }
    const rows = (await res.json()) as LeadSummary[];
    const raw = res.headers.get("X-Total-Count");
    const total = raw !== null && raw !== "" && !Number.isNaN(Number(raw))
      ? Number(raw) : rows.length;
    return { rows, total };
  },

  /** Global tag counts for the user views (hidden + skip excluded) — one SQL
   * GROUP BY, so the Companies chip bar never downloads the whole list. */
  listTags(): Promise<{ tag: string; count: number }[]> {
    return request<{ tag: string; count: number }[]>("/leads/tags");
  },

  /** Every query-run source label, for the Run/source dropdown. */
  listSources(): Promise<string[]> {
    return request<string[]>("/leads/sources");
  },

  // Organization (Phase B): set one lead's folder/tags.
  organizeLead(email: string, input: OrganizeInput): Promise<LeadSummary> {
    return request<LeadSummary>(`/leads/${encodeURIComponent(email)}/organize`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    });
  },

  // Folders catalog (Phase B.2/B.3): a folder is a persisted, clickable group —
  // empty folders exist FIRST, fill as leads are moved into them. Returns the
  // mailbox overview (folders + unfiled + total) so the Companies rail + All
  // chip + Dashboard stay honest.
  listFolders(): Promise<FoldersOut> {
    return request<FoldersOut>("/leads/folders");
  },

  /** Every extraction date (YYYY-MM-DD) on any dossier, newest first — the
   * global "kis tareekh ko kya nikla" recall options (folderized included). */
  listDates(): Promise<string[]> {
    return request<string[]>("/leads/dates");
  },

  /** Create a folder (idempotent). Returns the row + whether it was new. */
  createFolder(name: string): Promise<FolderCreateOut> {
    return request<FolderCreateOut>("/leads/folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  },

  /** Rename a folder or tag across every lead. */
  renameOrganize(kind: "folder" | "tag", from_: string, to: string): Promise<{ updated: number }> {
    return request("/leads/organize/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, from_, to }),
    });
  },

  /** Remove a folder or tag value from every lead. */
  clearOrganize(kind: "folder" | "tag", value: string): Promise<{ updated: number }> {
    return request("/leads/organize/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, value }),
    });
  },

  getLead(email: string): Promise<LeadDetail> {
    return request<LeadDetail>(`/leads/${encodeURIComponent(email)}`);
  },

  // Data management (user-controlled): a lead the user dismisses is gone.
  deleteLead(email: string): Promise<{ email: string; deleted: boolean }> {
    return request(`/leads/${encodeURIComponent(email)}`, { method: "DELETE" });
  },

  /** Bulk-remove every junk dossier (re-gated skip: dead domains, low score…). */
  clearJunk(): Promise<{ removed: number; emails: string[] }> {
    return request("/leads/clear-junk", { method: "POST" });
  },

  exportCsv(filter: Pick<LeadsFilter, "recommendation"> = {}): string {
    const q = filter.recommendation
      ? `?recommendation=${encodeURIComponent(filter.recommendation)}`
      : "";
    return `${BASE}/leads/export.csv${q}`;
  },

  // Fetch CSV text (sends the API key header) so the client can trigger a
  // same-origin blob download — works even when the backend enforces a key.
  async exportCsvData(
    filter: ExportFilter = {},
  ): Promise<string> {
    const params = new URLSearchParams();
    if (filter.recommendation) params.set("recommendation", filter.recommendation);
    if (filter.folder) params.set("folder", filter.folder);
    if (filter.tag) params.set("tag", filter.tag);
    if (filter.date) params.set("date", filter.date);
    if (filter.source) params.set("source", filter.source);
    if (filter.bound !== undefined) params.set("bound", String(filter.bound));
    if (filter.min_score !== undefined) params.set("min_score", String(filter.min_score));
    if (filter.q) params.set("q", filter.q);
    if (filter.emails && filter.emails.length > 0) params.set("emails", filter.emails.join(","));
    const qs = params.toString();
    return request<string>(`/leads/export.csv${qs ? `?${qs}` : ""}`, {
      headers: { Accept: "text/csv" },
    }, true);
  },
};
