import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity as ActivityIcon,
  AlertTriangle,
  Database,
  Eye,
  EyeOff,
  Gauge,
  KeyRound,
  RefreshCw,
  Share2,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users as UsersIcon,
} from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";
import { api } from "../api/client";
import { Spinner } from "../components/StatusChip";
import type {
  AdminLeadScope,
  AdminLeadScopeKind,
  AdminUser,
  AdminVisibilityDate,
} from "../types";

const cardClass =
  "rounded-xl border border-white/5 bg-white/[0.02] p-5";

const inputClass =
  "w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-1.5 text-[13px] text-slate-200 placeholder:text-slate-600 focus:border-indigo-400/60 focus:outline-none";

function count(values: Record<string, number>, key: string): string {
  return (values[key] ?? 0).toLocaleString();
}

type Tab = "overview" | "users" | "data" | "keys" | "caches" | "audit";

const TABS: { id: Tab; label: string; icon: typeof ShieldCheck }[] = [
  { id: "overview", label: "Overview", icon: Gauge },
  { id: "users", label: "Users & Activity", icon: UsersIcon },
  { id: "data", label: "Data Control", icon: EyeOff },
  { id: "keys", label: "API Keys", icon: KeyRound },
  { id: "caches", label: "Caches", icon: Database },
  { id: "audit", label: "Audit Log", icon: ActivityIcon },
];

export default function Admin() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("overview");

  const dashboard = useQuery({
    queryKey: ["admin-dashboard"],
    queryFn: () => api.adminDashboard(),
    refetchInterval: 10_000,
  });
  const keys = useQuery({
    queryKey: ["admin-keys"],
    queryFn: () => api.adminKeys(),
    refetchInterval: 30_000,
  });
  const deleted = useQuery({
    queryKey: ["admin-deleted"],
    queryFn: () => api.adminDeleted(),
  });
  const pendingCache = useQuery({
    queryKey: ["admin-pending-cache"],
    queryFn: () => api.adminPendingCache(),
  });
  const searchCache = useQuery({
    queryKey: ["admin-search-cache"],
    queryFn: () => api.adminSearchCache(),
  });
  const visibility = useQuery({
    queryKey: ["admin-visibility"],
    queryFn: () => api.adminVisibility(),
    refetchInterval: 10_000,
  });
  const users = useQuery({
    queryKey: ["admin-users"],
    queryFn: () => api.adminUsers(),
  });

  const invalidateVisibility = () => {
    qc.invalidateQueries({ queryKey: ["admin-visibility"] });
    qc.invalidateQueries({ queryKey: ["admin-dashboard"] });
  };
  const hideLeads = useMutation({
    mutationFn: (body: AdminLeadScope) => api.adminHideLeads(body),
    onSuccess: invalidateVisibility,
  });
  const showLeads = useMutation({
    mutationFn: (body: AdminLeadScope) => api.adminShowLeads(body),
    onSuccess: invalidateVisibility,
  });
  const deleteLeads = useMutation({
    mutationFn: (body: AdminLeadScope) => api.adminDeleteLeads(body),
    onSuccess: invalidateVisibility,
  });

  const [otherScope, setOtherScope] = useState<AdminLeadScopeKind>("source");
  const [otherValue, setOtherValue] = useState("");

  const updateKey = useMutation({
    mutationFn: ({ name, value }: { name: string; value: string }) =>
      api.updateAdminKey(name, value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-keys"] }),
  });
  const purge = useMutation({
    mutationFn: () => api.purgeSearchCache(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-search-cache"] });
      qc.invalidateQueries({ queryKey: ["admin-dashboard"] });
    },
  });

  const data = dashboard.data;
  const keysData = keys.data;
  const allUsers = users.data?.users ?? [];

  const refreshAll = () => {
    dashboard.refetch();
    keys.refetch();
    deleted.refetch();
    pendingCache.refetch();
    searchCache.refetch();
    visibility.refetch();
    users.refetch();
    qc.invalidateQueries({ queryKey: ["admin-activity"] });
  };

  return (
    <div className="px-8 py-7 max-w-6xl">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-indigo-400" />
            <h1 className="text-[26px] font-semibold text-white">Admin Operations</h1>
          </div>
          <p className="text-slate-500 text-[13.5px] mt-1">
            Users, activity, data control, keys and caches — everything the admin role manages.
          </p>
        </div>
        <button
          onClick={refreshAll}
          className="rounded-lg border border-white/5 px-3 py-2 text-[13px] text-slate-300 hover:bg-white/[0.04] inline-flex items-center gap-2"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      {/* Tab bar */}
      <div className="mt-5 flex flex-wrap gap-1.5 border-b border-white/5 pb-0">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`inline-flex items-center gap-2 rounded-t-lg border-b-2 px-3.5 py-2.5 text-[13px] transition-colors ${
              tab === id
                ? "border-indigo-500 bg-white/[0.03] font-medium text-white"
                : "border-transparent text-slate-500 hover:text-slate-300"
            }`}
          >
            <Icon className="h-3.5 w-3.5" /> {label}
          </button>
        ))}
      </div>

      {/* -------------------------------------------------------------- */}
      {/* OVERVIEW */}
      {/* -------------------------------------------------------------- */}
      {tab === "overview" && (
        <>
          {dashboard.isLoading && (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-slate-500">
              <Spinner /> Loading admin metrics…
            </div>
          )}
          {dashboard.isError && (
            <p className="mt-5 rounded-lg bg-rose-500/10 px-3 py-2 text-[13px] text-rose-300">
              Admin data unavailable: {(dashboard.error as Error).message}
            </p>
          )}
          {data && (
            <>
              <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <Metric label="Dossiers" value={data.dossiers_total} icon={Database} />
                <Metric label="Contact now" value={count(data.recommendations, "contact_now")} icon={Gauge} />
                <Metric label="Pending active" value={count(data.pending, "active")} icon={RefreshCw} />
                <Metric label="Failed jobs" value={count(data.jobs, "failed")} icon={AlertTriangle} danger />
              </div>

              <div className="mt-6 grid grid-cols-1 gap-5 lg:grid-cols-2">
                <section className={cardClass}>
                  <h2 className="text-[16px] font-semibold text-white">Lead quality</h2>
                  <div className="mt-4 grid grid-cols-3 gap-3">
                    <Value label="Nurture" value={count(data.recommendations, "nurture")} />
                    <Value label="Skip" value={count(data.recommendations, "skip")} />
                    <Value label="Dead pending" value={count(data.pending, "dead")} />
                  </div>
                </section>
                <section className={cardClass}>
                  <h2 className="text-[16px] font-semibold text-white">Risks</h2>
                  <div className="mt-4 grid grid-cols-2 gap-3">
                    <Value label="Source errors" value={count(data.risks, "source_errors")} danger />
                    <Value label="Missing evidence" value={count(data.risks, "missing_evidence")} danger />
                    <Value label="Unbound contacts" value={count(data.risks, "unbound_contacts")} />
                    <Value label="Missing company" value={count(data.risks, "missing_company")} />
                  </div>
                </section>
              </div>

              <section className={`${cardClass} mt-5`}>
                <h2 className="text-[16px] font-semibold text-white">Recent jobs</h2>
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full text-left text-[13px]">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="pb-2 pr-4 font-medium">Query</th>
                        <th className="pb-2 pr-4 font-medium">State</th>
                        <th className="pb-2 pr-4 font-medium">Elapsed</th>
                        <th className="pb-2 font-medium">Error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.recent_jobs.map((job) => (
                        <tr key={job.id} className="border-t border-white/5">
                          <td className="py-3 pr-4 text-slate-200">
                            {job.query.trade || "—"} · {job.query.location || "—"}
                          </td>
                          <td className={`py-3 pr-4 ${job.state === "failed" ? "text-rose-300" : "text-slate-300"}`}>
                            {job.state}
                          </td>
                          <td className="py-3 pr-4 text-slate-400">{Math.round(job.elapsed_s)}s</td>
                          <td className="max-w-xs truncate py-3 text-slate-500">{job.error || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {data.recent_jobs.length === 0 && (
                    <p className="py-5 text-[13px] text-slate-500">No jobs recorded.</p>
                  )}
                </div>
                <p className="mt-4 truncate text-[11px] text-slate-600">Database: {data.database_path}</p>
              </section>
            </>
          )}
        </>
      )}

      {/* -------------------------------------------------------------- */}
      {/* USERS & ACTIVITY */}
      {/* -------------------------------------------------------------- */}
      {tab === "users" && (
        <UsersTab users={allUsers} usersLoading={users.isLoading} usersError={users.error as Error | null} />
      )}

      {/* -------------------------------------------------------------- */}
      {/* DATA CONTROL */}
      {/* -------------------------------------------------------------- */}
      {tab === "data" && (
        <>
          <section className={`${cardClass} mt-6`}>
            <div className="flex items-center gap-2">
              <EyeOff className="h-4 w-4 text-indigo-400" />
              <h2 className="text-[16px] font-semibold text-white">Dashboard data control</h2>
            </div>
            <p className="mt-1 text-[12px] text-slate-500">
              Control which researched data the USER dashboard / Companies screen shows.{" "}
              <span className="text-slate-300">Hide</span> removes a date/search/lead from the user views
              (reversible via <span className="text-slate-300">Show</span>);{" "}
              <span className="text-rose-300">Delete</span> permanently removes it from the server
              (irreversible). The admin panel always sees everything.
            </p>

            {visibility.isError && (
              <p className="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-[13px] text-rose-300">
                Visibility unavailable: {(visibility.error as Error).message}
              </p>
            )}

            {visibility.data && (
              <div className="mt-4 flex gap-3 text-[13px]">
                <Value label="Total researched" value={visibility.data.total.toLocaleString()} />
                <Value label="Hidden (off user dashboard)" value={visibility.data.hidden.toLocaleString()} danger={visibility.data.hidden > 0} />
              </div>
            )}

            <div className="mt-4 overflow-x-auto">
              <table className="w-full text-left text-[12.5px]">
                <thead className="text-slate-500">
                  <tr>
                    <th className="pb-2 pr-4 font-medium">Date searched</th>
                    <th className="pb-2 pr-4 font-medium">Visible</th>
                    <th className="pb-2 pr-4 font-medium">Hidden</th>
                    <th className="pb-2 font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {(visibility.data?.by_date ?? []).map((row: AdminVisibilityDate) => (
                    <tr key={row.date} className="border-t border-white/5">
                      <td className="py-2.5 pr-4 text-slate-200">{row.date}</td>
                      <td className="py-2.5 pr-4 text-slate-400">{(row.total - row.hidden).toLocaleString()}</td>
                      <td className="py-2.5 pr-4 text-slate-400">{row.hidden.toLocaleString()}</td>
                      <td className="py-2.5">
                        <div className="flex items-center gap-1.5">
                          <RowButton
                            label="Hide"
                            icon={<EyeOff className="h-3.5 w-3.5" />}
                            tint="indigo"
                            busy={hideLeads.isPending}
                            onClick={() => hideLeads.mutate({ scope: "date", value: row.date })}
                          />
                          {row.hidden > 0 && (
                            <RowButton
                              label="Show"
                              icon={<Eye className="h-3.5 w-3.5" />}
                              tint="emerald"
                              busy={showLeads.isPending}
                              onClick={() => showLeads.mutate({ scope: "date", value: row.date })}
                            />
                          )}
                          <RowButton
                            label="Delete"
                            icon={<Trash2 className="h-3.5 w-3.5" />}
                            tint="rose"
                            busy={deleteLeads.isPending}
                            onClick={() => {
                              if (window.confirm(`SERIOUSLY delete ALL ${row.total} leads from ${row.date}? This is permanent.`)) {
                                deleteLeads.mutate({ scope: "date", value: row.date });
                              }
                            }}
                          />
                        </div>
                      </td>
                    </tr>
                  ))}
                  {(visibility.data?.by_date ?? []).length === 0 && !visibility.isLoading && (
                    <tr><td colSpan={4} className="py-4 text-slate-500">No researched data yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>

            {/* Other scope — a single search run, folder, or one email */}
            <div className="mt-5 border-t border-white/5 pt-4">
              <h3 className="text-[13px] font-medium text-slate-300">Act on a search, folder or one lead</h3>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <select
                  value={otherScope}
                  onChange={(e) => setOtherScope(e.target.value as AdminLeadScopeKind)}
                  className={inputClass + " w-auto"}
                >
                  <option value="source">By search (trade · location)</option>
                  <option value="folder">By folder</option>
                  <option value="email">By email</option>
                </select>
                <input
                  value={otherValue}
                  onChange={(e) => setOtherValue(e.target.value)}
                  placeholder={otherScope === "email" ? "owner@company.com" : otherScope === "folder" ? 'e.g. Q3 Outreach' : 'e.g. General Contractors · Dallas TX'}
                  className={inputClass + " w-72"}
                />
                <button
                  onClick={() => hideLeads.mutate({ scope: otherScope, value: otherValue.trim() })}
                  disabled={!otherValue.trim() || hideLeads.isPending}
                  className="rounded-lg border border-indigo-400/20 px-3 py-1.5 text-[12.5px] text-indigo-300 hover:bg-indigo-500/10 disabled:opacity-40 inline-flex items-center gap-2"
                >
                  <EyeOff className="h-3.5 w-3.5" /> Hide
                </button>
                <button
                  onClick={() => showLeads.mutate({ scope: otherScope, value: otherValue.trim() })}
                  disabled={!otherValue.trim() || showLeads.isPending}
                  className="rounded-lg border border-emerald-400/20 px-3 py-1.5 text-[12.5px] text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-40 inline-flex items-center gap-2"
                >
                  <Eye className="h-3.5 w-3.5" /> Show
                </button>
                <button
                  onClick={() => {
                    if (window.confirm(`SERIOUSLY delete ${otherScope === "email" ? "this lead" : "every lead in this scope"}? This is permanent.`)) {
                      deleteLeads.mutate({ scope: otherScope, value: otherValue.trim() });
                    }
                  }}
                  disabled={!otherValue.trim() || deleteLeads.isPending}
                  className="rounded-lg border border-rose-400/20 px-3 py-1.5 text-[12.5px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-40 inline-flex items-center gap-2"
                >
                  <Trash2 className="h-3.5 w-3.5" /> Delete
                </button>
              </div>
            </div>

            {(hideLeads.data || showLeads.data || deleteLeads.data) && (
              <p className="mt-3 text-[12px] text-emerald-300">
                {(hideLeads.data?.affected ?? showLeads.data?.affected ?? deleteLeads.data?.affected ?? 0) > 0
                  ? `${hideLeads.data?.affected ?? showLeads.data?.affected ?? deleteLeads.data?.affected ?? 0} lead(s) updated.`
                  : "Nothing matched that scope."}
              </p>
            )}
          </section>

          <AssignCard users={allUsers} />
        </>
      )}

      {/* -------------------------------------------------------------- */}
      {/* API KEYS */}
      {/* -------------------------------------------------------------- */}
      {tab === "keys" && (
        <section className={`${cardClass} mt-6`}>
          <div className="flex items-center gap-2">
            <KeyRound className="h-4 w-4 text-amber-300" />
            <h2 className="text-[16px] font-semibold text-white">API keys</h2>
          </div>
          <p className="mt-1 text-[12px] text-slate-500">
            Add / clear provider keys without editing .env. Only a masked tail is shown —
            a full secret never leaves the backend. Changes apply immediately (search
            providers are rebuilt with the new key) — no backend restart needed.
          </p>

          {keys.isLoading && (
            <div className="mt-4 flex items-center gap-2 py-4 text-[13px] text-slate-500">
              <Spinner /> Loading keys…
            </div>
          )}
          {keys.isError && (
            <p className="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-[13px] text-rose-300">
              Keys unavailable: {(keys.error as Error).message}
            </p>
          )}

          {keysData && (
            <>
              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-left text-[13px]">
                  <thead className="text-slate-500">
                    <tr>
                      <th className="pb-2 pr-4 font-medium">Key</th>
                      <th className="pb-2 pr-4 font-medium">Status</th>
                      <th className="pb-2 pr-4 font-medium">Masked</th>
                      <th className="pb-2 font-medium">Set / clear</th>
                    </tr>
                  </thead>
                  <tbody>
                    {keysData.keys.map((k) => (
                      <KeyRow
                        key={k.name}
                        name={k.name}
                        configured={k.configured}
                        masked={k.masked}
                        busy={updateKey.isPending}
                        onSave={(value) => updateKey.mutate({ name: k.name, value })}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="mt-4 grid grid-cols-1 gap-2 text-[12px] text-slate-500 sm:grid-cols-2 lg:grid-cols-4">
                <span>Provider: <span className="text-slate-300">{keysData.provider}</span></span>
                <span>Model: <span className="text-slate-300">{keysData.model}</span></span>
                <span>Base URL: <span className="text-slate-300 truncate">{keysData.base_url}</span></span>
                <span>SearXNG: <span className="text-slate-300 truncate">{keysData.searxng_url || "not configured"}</span></span>
                <span>Env: <span className="text-slate-300">{keysData.env}</span></span>
              </div>
              {keysData.applies_after_restart && (
                <p className="mt-3 truncate text-[11px] text-amber-300/80">
                  ⚠ Changes persist to {keysData.overlay_path} and apply after a backend restart.
                </p>
              )}
            </>
          )}
        </section>
      )}

      {/* -------------------------------------------------------------- */}
      {/* CACHES */}
      {/* -------------------------------------------------------------- */}
      {tab === "caches" && (
        <>
          <section className={`${cardClass} mt-6`}>
            <h2 className="text-[16px] font-semibold text-white">Discovery cache — pending leads</h2>
            <p className="mt-1 text-[12px] text-slate-500">
              Emails sitting in the discovery cache, waiting to be researched (kon kon si
              email cache mein pari hai). Dead = never served again (no more credits burned).
            </p>
            {pendingCache.data && (
              <div className="mt-3 flex gap-3 text-[13px]">
                <Value label="Total" value={pendingCache.data.total.toLocaleString()} />
                <Value label="Active" value={pendingCache.data.active.toLocaleString()} />
                <Value label="Dead" value={pendingCache.data.dead.toLocaleString()} danger />
              </div>
            )}
            <div className="mt-4 max-h-64 overflow-auto">
              <table className="w-full text-left text-[12.5px]">
                <thead className="sticky top-0 bg-[#0b1020] text-slate-500">
                  <tr>
                    <th className="pb-2 pr-4 font-medium">Email</th>
                    <th className="pb-2 pr-4 font-medium">Location</th>
                    <th className="pb-2 pr-4 font-medium">Retries</th>
                    <th className="pb-2 font-medium">Last attempt</th>
                  </tr>
                </thead>
                <tbody>
                  {(pendingCache.data?.rows ?? []).map((row) => (
                    <tr key={row.email} className={`border-t border-white/5 ${row.dead ? "opacity-50" : ""}`}>
                      <td className="py-2 pr-4 text-slate-300">
                        {row.dead && <span className="mr-1.5 text-slate-500">[dead]</span>}
                        {row.email}
                      </td>
                      <td className="py-2 pr-4 text-slate-400">{row.location || "—"}</td>
                      <td className="py-2 pr-4 text-slate-400">{row.attempt_count}</td>
                      <td className="py-2 text-slate-500">{row.attempted_at || "never"}</td>
                    </tr>
                  ))}
                  {(pendingCache.data?.rows ?? []).length === 0 && !pendingCache.isLoading && (
                    <tr><td colSpan={4} className="py-4 text-slate-500">Discovery cache is empty.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className={`${cardClass} mt-5`}>
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-[16px] font-semibold text-white">Search cache</h2>
                <p className="mt-1 text-[12px] text-slate-500">
                  The provider-neutral disk cache that stops paid query repeats. Rows =
                  distinct cached queries / extracted pages.
                </p>
              </div>
              <button
                onClick={() => purge.mutate()}
                disabled={purge.isPending}
                className="ml-auto rounded-lg border border-rose-400/20 px-3 py-1.5 text-[12.5px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 inline-flex items-center gap-2"
                title="Remove TTL-expired entries only (a paid query re-fills them)"
              >
                {purge.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
                Purge expired
              </button>
            </div>
            {searchCache.data && (
              <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <Value label="Cached queries" value={searchCache.data.search_rows.toLocaleString()} />
                <Value label="Cached extracts" value={searchCache.data.extract_rows.toLocaleString()} />
                <Value label="Hit rate" value={`${Math.round(searchCache.data.hit_rate * 100)}%`} />
                <Value label="TTL" value={`${searchCache.data.search_ttl_days}d / ${searchCache.data.extract_ttl_days}d`} />
              </div>
            )}
            {searchCache.data && searchCache.data.top_queries.length > 0 && (
              <>
                <h3 className="mt-5 text-[13px] font-medium text-slate-300">Recent cached queries</h3>
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full text-left text-[12.5px]">
                    <thead className="text-slate-500">
                      <tr>
                        <th className="pb-2 pr-4 font-medium">Query</th>
                        <th className="pb-2 pr-4 font-medium">Max results</th>
                        <th className="pb-2 font-medium">Cached at</th>
                      </tr>
                    </thead>
                    <tbody>
                      {searchCache.data.top_queries.map((q, i) => (
                        <tr key={i} className="border-t border-white/5">
                          <td className="max-w-md truncate py-2 pr-4 text-slate-300">{q.query}</td>
                          <td className="py-2 pr-4 text-slate-400">{q.max_results}</td>
                          <td className="py-2 text-slate-500">{q.fetched_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
            {purge.data && (
              <p className="mt-3 text-[12px] text-emerald-300">
                Purged {purge.data.removed} expired cache entr{purge.data.removed === 1 ? "y" : "ies"}.
              </p>
            )}
          </section>
        </>
      )}

      {/* -------------------------------------------------------------- */}
      {/* AUDIT LOG */}
      {/* -------------------------------------------------------------- */}
      {tab === "audit" && (
        <section className={`${cardClass} mt-6`}>
          <h2 className="text-[16px] font-semibold text-white">Deleted leads — audit log</h2>
          <p className="mt-1 text-[12px] text-slate-500">
            Which emails were deleted, when, and why (manual Delete vs Junk sweep).
          </p>
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-left text-[13px]">
              <thead className="text-slate-500">
                <tr>
                  <th className="pb-2 pr-4 font-medium">Email</th>
                  <th className="pb-2 pr-4 font-medium">Deleted at</th>
                  <th className="pb-2 font-medium">Reason</th>
                </tr>
              </thead>
              <tbody>
                {(deleted.data?.deleted ?? []).map((row) => (
                  <tr key={row.email} className="border-t border-white/5">
                    <td className="py-2 pr-4 text-slate-300">{row.email}</td>
                    <td className="py-2 pr-4 text-slate-400">{row.deleted_at}</td>
                    <td className="py-2">
                      <span className={`rounded-full px-2 py-0.5 text-[11.5px] ${
                        row.reason === "junk" ? "bg-slate-500/10 text-slate-400" : "bg-rose-500/10 text-rose-300"
                      }`}>
                        {row.reason}
                      </span>
                    </td>
                  </tr>
                ))}
                {(deleted.data?.deleted ?? []).length === 0 && !deleted.isLoading && (
                  <tr><td colSpan={3} className="py-4 text-slate-500">Nothing deleted yet.</td></tr>
                )}
              </tbody>
            </table>
            {deleted.data && (
              <p className="mt-3 text-[11px] text-slate-600">{deleted.data.total} total deleted</p>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Users & Activity tab
// ---------------------------------------------------------------------------

function UsersTab({
  users,
  usersLoading,
  usersError,
}: {
  users: AdminUser[];
  usersLoading: boolean;
  usersError: Error | null;
}) {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [activityFilter, setActivityFilter] = useState<string>("");

  const activity = useQuery({
    queryKey: ["admin-activity", activityFilter],
    queryFn: () => api.adminActivity(200, activityFilter),
    refetchInterval: 15_000,
  });

  const summary = useQuery({
    queryKey: ["admin-user-summary", expanded],
    queryFn: () => api.adminUserLeadSummary(expanded!),
    enabled: !!expanded,
  });

  const invalidateUsers = () => {
    qc.invalidateQueries({ queryKey: ["admin-users"] });
    qc.invalidateQueries({ queryKey: ["admin-activity"] });
  };

  const createUser = useMutation({
    mutationFn: (body: { username: string; email: string; password: string }) =>
      api.adminCreateUser(body),
    onSuccess: invalidateUsers,
  });
  const deleteUser = useMutation({
    mutationFn: (userId: string) => api.adminDeleteUser(userId),
    onSuccess: invalidateUsers,
  });
  const resetPassword = useMutation({
    mutationFn: ({ userId, password }: { userId: string; password: string }) =>
      api.adminResetUserPassword(userId, password),
  });

  return (
    <>
      {/* Create account */}
      <section className={`${cardClass} mt-6`}>
        <div className="flex items-center gap-2">
          <UserPlus className="h-4 w-4 text-emerald-400" />
          <h2 className="text-[16px] font-semibold text-white">Add an account</h2>
        </div>
        <p className="mt-1 text-[12px] text-slate-500">
          Create a user account directly — the user logs in with this username + password.
        </p>
        <CreateUserForm busy={createUser.isPending} error={createUser.error as Error | null} onCreate={(b) => createUser.mutate(b)} />
        {createUser.data && (
          <p className="mt-3 text-[12.5px] text-emerald-300">
            Account “{createUser.data.username}” created — the user can log in now.
          </p>
        )}
      </section>

      {/* Accounts table */}
      <section className={`${cardClass} mt-5`}>
        <div className="flex items-center gap-2">
          <UsersIcon className="h-4 w-4 text-indigo-400" />
          <h2 className="text-[16px] font-semibold text-white">Accounts</h2>
        </div>
        <p className="mt-1 text-[12px] text-slate-500">
          Click a row to see that user's data (folders, dates, activity). Reset password or
          delete accounts here — a deleted account's data stays on the server (admin-visible only).
        </p>

        {usersError && (
          <p className="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-[13px] text-rose-300">
            Users unavailable: {usersError.message}
          </p>
        )}
        {usersLoading && (
          <div className="mt-4 flex items-center gap-2 text-[13px] text-slate-500">
            <Spinner /> Loading accounts…
          </div>
        )}

        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-[12.5px]">
            <thead className="text-slate-500">
              <tr>
                <th className="pb-2 pr-4 font-medium">Username</th>
                <th className="pb-2 pr-4 font-medium">Email</th>
                <th className="pb-2 pr-4 font-medium">Role</th>
                <th className="pb-2 pr-4 font-medium">Created</th>
                <th className="pb-2 font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <UserRow
                  key={u.id}
                  user={u}
                  expanded={expanded === u.id}
                  onToggle={() => setExpanded(expanded === u.id ? null : u.id)}
                  summary={summary.data}
                  summaryLoading={summary.isLoading && expanded === u.id}
                  onResetPassword={() => {
                    const password = window.prompt(`New password for "${u.username}":`);
                    if (password && password.trim()) {
                      resetPassword.mutate({ userId: u.id, password: password.trim() });
                    }
                  }}
                  resetPending={resetPassword.isPending}
                  onDelete={() => {
                    if (window.confirm(`Delete account "${u.username}"? Their data stays on the server (admin-visible only).`)) {
                      deleteUser.mutate(u.id);
                    }
                  }}
                  deletePending={deleteUser.isPending}
                />
              ))}
              {users.length === 0 && !usersLoading && (
                <tr><td colSpan={5} className="py-4 text-slate-500">No accounts yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        {resetPassword.data && (
          <p className="mt-3 text-[12.5px] text-emerald-300">
            Password reset for “{resetPassword.data.username}”.
          </p>
        )}
      </section>

      {/* Activity log */}
      <section className={`${cardClass} mt-5`}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <ActivityIcon className="h-4 w-4 text-sky-400" />
            <h2 className="text-[16px] font-semibold text-white">Activity log</h2>
          </div>
          <select
            value={activityFilter}
            onChange={(e) => setActivityFilter(e.target.value)}
            className={inputClass + " w-auto"}
          >
            <option value="">All users</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.username}
              </option>
            ))}
          </select>
        </div>
        <p className="mt-1 text-[12px] text-slate-500">
          Logins, logouts, signups and searches — who did what, newest first.
        </p>
        <div className="mt-4 max-h-80 overflow-auto">
          <table className="w-full text-left text-[12.5px]">
            <thead className="sticky top-0 bg-[#0b1020] text-slate-500">
              <tr>
                <th className="pb-2 pr-4 font-medium">When</th>
                <th className="pb-2 pr-4 font-medium">User</th>
                <th className="pb-2 pr-4 font-medium">Action</th>
                <th className="pb-2 font-medium">Detail</th>
              </tr>
            </thead>
            <tbody>
              {(activity.data?.activity ?? []).map((row) => (
                <tr key={row.id} className="border-t border-white/5">
                  <td className="py-2 pr-4 text-slate-500 whitespace-nowrap">{row.created_at}</td>
                  <td className="py-2 pr-4 text-slate-300">{row.username}</td>
                  <td className="py-2 pr-4">
                    <span className={`rounded-full px-2 py-0.5 text-[11.5px] ${
                      row.action === "login"
                        ? "bg-emerald-500/10 text-emerald-300"
                        : row.action === "logout"
                          ? "bg-slate-500/10 text-slate-400"
                          : row.action === "search"
                            ? "bg-indigo-500/10 text-indigo-300"
                            : "bg-sky-500/10 text-sky-300"
                    }`}>
                      {row.action}
                    </span>
                  </td>
                  <td className="py-2 text-slate-400">{row.detail || "—"}</td>
                </tr>
              ))}
              {(activity.data?.activity ?? []).length === 0 && !activity.isLoading && (
                <tr><td colSpan={4} className="py-4 text-slate-500">No activity recorded yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}

function CreateUserForm({
  busy,
  error,
  onCreate,
}: {
  busy: boolean;
  error: Error | null;
  onCreate: (body: { username: string; email: string; password: string }) => void;
}) {
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  return (
    <div className="mt-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="username"
          className={inputClass}
        />
        <input
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="email"
          className={inputClass}
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="password"
          className={inputClass}
          autoComplete="new-password"
        />
      </div>
      <div className="mt-2 flex items-center gap-3">
        <button
          onClick={() => {
            onCreate({ username: username.trim(), email: email.trim(), password });
            setUsername("");
            setEmail("");
            setPassword("");
          }}
          disabled={busy || !username.trim() || !email.trim() || !password}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-2"
        >
          {busy ? <Spinner className="h-3.5 w-3.5" /> : <UserPlus className="h-3.5 w-3.5" />}
          Create account
        </button>
        {error && <span className="text-[12.5px] text-rose-300">{error.message}</span>}
      </div>
    </div>
  );
}

function UserRow({
  user,
  expanded,
  onToggle,
  summary,
  summaryLoading,
  onResetPassword,
  resetPending,
  onDelete,
  deletePending,
}: {
  user: AdminUser;
  expanded: boolean;
  onToggle: () => void;
  summary: import("../types").AdminUserLeadSummary | undefined;
  summaryLoading: boolean;
  onResetPassword: () => void;
  resetPending: boolean;
  onDelete: () => void;
  deletePending: boolean;
}) {
  return (
    <>
      <tr className={`border-t border-white/5 ${expanded ? "bg-white/[0.02]" : "hover:bg-white/[0.02]"}`}>
        <td className="py-2.5 pr-4">
          <button onClick={onToggle} className="text-slate-200 hover:text-white text-left">
            <span className={`inline-block transition-transform mr-1.5 text-slate-500 ${expanded ? "rotate-90" : ""}`}>›</span>
            {user.username}
          </button>
        </td>
        <td className="py-2.5 pr-4 text-slate-400">{user.email}</td>
        <td className="py-2.5 pr-4">
          <span className={`rounded-full px-2 py-0.5 text-[11.5px] ${
            user.is_admin ? "bg-amber-500/10 text-amber-300" : "bg-slate-500/10 text-slate-400"
          }`}>
            {user.is_admin ? "admin" : "user"}
          </span>
        </td>
        <td className="py-2.5 pr-4 text-slate-500 whitespace-nowrap">{user.created_at.slice(0, 10)}</td>
        <td className="py-2.5">
          <div className="flex items-center gap-1.5">
            <RowButton
              label="Reset password"
              icon={<KeyRound className="h-3.5 w-3.5" />}
              tint="indigo"
              busy={resetPending}
              onClick={onResetPassword}
            />
            {!user.is_admin && (
              <RowButton
                label="Delete"
                icon={<Trash2 className="h-3.5 w-3.5" />}
                tint="rose"
                busy={deletePending}
                onClick={onDelete}
              />
            )}
          </div>
        </td>
      </tr>
      {expanded && (
        <tr className="border-t border-white/5 bg-black/20">
          <td colSpan={5} className="px-4 py-3">
            {summaryLoading && (
              <div className="flex items-center gap-2 text-[13px] text-slate-500">
                <Spinner /> Loading {user.username}'s data…
              </div>
            )}
            {summary && (
              <div>
                <p className="text-[12px] text-slate-500 mb-2">
                  {user.username}'s dashboard — {summary.total.toLocaleString()} visible lead(s)
                  ({summary.unfiled.toLocaleString()} unfiled)
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(summary.folders).map(([name, c]) => (
                    <span key={name} className="rounded-md border border-white/5 bg-white/[0.02] px-2 py-0.5 text-[11.5px] text-slate-400">
                      {name} · {c}
                    </span>
                  ))}
                  {summary.dates.length > 0 && (
                    <span className="rounded-md border border-white/5 bg-white/[0.02] px-2 py-0.5 text-[11.5px] text-slate-500">
                      dates: {summary.dates.slice(0, 6).join(", ")}
                      {summary.dates.length > 6 ? "…" : ""}
                    </span>
                  )}
                  {summary.total === 0 && (
                    <span className="text-[12px] text-slate-500">No data on this user's dashboard yet.</span>
                  )}
                </div>
              </div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Assign-to-user card (Data Control tab) — the "push my folder to a user" flow
// ---------------------------------------------------------------------------

function AssignCard({ users }: { users: AdminUser[] }) {
  const qc = useQueryClient();
  const [scope, setScope] = useState<AdminLeadScopeKind>("folder");
  const [value, setValue] = useState("");
  const [userId, setUserId] = useState("");

  const assign = useMutation({
    mutationFn: (body: { scope: AdminLeadScopeKind; value: string; user_id: string }) =>
      api.adminAssignLeads(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-user-summary"] });
      qc.invalidateQueries({ queryKey: ["admin-visibility"] });
    },
  });

  const nonAdminUsers = users.filter((u) => !u.is_admin);
  const ready = value.trim() && userId;

  return (
    <section className={`${cardClass} mt-5`}>
      <div className="flex items-center gap-2">
        <Share2 className="h-4 w-4 text-sky-400" />
        <h2 className="text-[16px] font-semibold text-white">Push data to a user's dashboard</h2>
      </div>
      <p className="mt-1 text-[12px] text-slate-500">
        Assign a folder / date / search / single lead to a user — it appears on their dashboard
        with one click (e.g. your 50-email folder → a user's Companies screen). “Take back”
        removes it from their dashboard again; the admin panel always sees everything.
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <select
          value={scope}
          onChange={(e) => setScope(e.target.value as AdminLeadScopeKind)}
          className={inputClass + " w-auto"}
        >
          <option value="folder">By folder</option>
          <option value="date">By date</option>
          <option value="source">By search (trade · location)</option>
          <option value="email">By email</option>
        </select>
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={scope === "email" ? "owner@company.com" : scope === "folder" ? 'e.g. Q3 Outreach' : scope === "date" ? "YYYY-MM-DD" : 'e.g. GC · Houston, TX'}
          className={inputClass + " w-64"}
        />
        <select
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          className={inputClass + " w-auto"}
        >
          <option value="" disabled>
            Select user…
          </option>
          {nonAdminUsers.map((u) => (
            <option key={u.id} value={u.id}>
              {u.username}
            </option>
          ))}
        </select>
        <button
          onClick={() => assign.mutate({ scope, value: value.trim(), user_id: userId })}
          disabled={!ready || assign.isPending}
          className="rounded-lg bg-sky-600 px-3.5 py-2 text-[12.5px] font-semibold text-white hover:bg-sky-500 disabled:opacity-40 inline-flex items-center gap-2"
        >
          {assign.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Share2 className="h-3.5 w-3.5" />}
          Push to user
        </button>
        <button
          onClick={() => assign.mutate({ scope, value: value.trim(), user_id: "" })}
          disabled={!value.trim() || assign.isPending}
          className="rounded-lg border border-white/10 px-3 py-2 text-[12.5px] text-slate-300 hover:bg-white/[0.04] disabled:opacity-40"
        >
          Take back (admin-only)
        </button>
      </div>
      {assign.data && (
        <p className="mt-3 text-[12px] text-emerald-300">
          {assign.data.affected > 0
            ? `${assign.data.affected} lead(s) reassigned.`
            : "Nothing matched that scope."}
        </p>
      )}
      {assign.isError && (
        <p className="mt-3 text-[12px] text-rose-300">{(assign.error as Error).message}</p>
      )}
      {nonAdminUsers.length === 0 && (
        <p className="mt-3 text-[12px] text-slate-500">
          No user accounts yet — create one in the Users & Activity tab first.
        </p>
      )}
    </section>
  );
}

/** One API-key row with an inline set/clear control (value never shown back). */
function KeyRow({
  name,
  configured,
  masked,
  busy,
  onSave,
}: {
  name: string;
  configured: boolean;
  masked: string;
  busy: boolean;
  onSave: (value: string) => void;
}) {
  const [value, setValue] = useState("");
  return (
    <tr className="border-t border-white/5">
      <td className="py-2.5 pr-4 text-slate-200">{name}</td>
      <td className="py-2.5 pr-4">
        <span className={`rounded-full px-2 py-0.5 text-[11.5px] ${
          configured ? "bg-emerald-500/10 text-emerald-300" : "bg-slate-500/10 text-slate-400"
        }`}>
          {configured ? "configured" : "not set"}
        </span>
      </td>
      <td className="py-2.5 pr-4 font-mono text-slate-400">{masked || "—"}</td>
      <td className="py-2.5">
        <div className="flex items-center gap-1.5">
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="new value (empty = clear)"
            className={inputClass}
            autoComplete="off"
          />
          <button
            onClick={() => onSave(value)}
            disabled={busy}
            className="rounded-lg border border-white/10 px-2.5 py-1.5 text-[12.5px] text-slate-200 hover:bg-white/[0.04] disabled:opacity-50 whitespace-nowrap"
          >
            {value.trim() ? "Save" : configured ? "Clear" : "Set"}
          </button>
        </div>
      </td>
    </tr>
  );
}

function RowButton({
  label,
  icon,
  tint,
  busy,
  onClick,
}: {
  label: string;
  icon: ReactNode;
  tint: "indigo" | "emerald" | "rose";
  busy: boolean;
  onClick: () => void;
}) {
  const tintClass =
    tint === "rose"
      ? "border-rose-400/20 text-rose-300 hover:bg-rose-500/10"
      : tint === "emerald"
        ? "border-emerald-400/20 text-emerald-300 hover:bg-emerald-500/10"
        : "border-indigo-400/20 text-indigo-300 hover:bg-indigo-500/10";
  return (
    <button
      onClick={onClick}
      disabled={busy}
      className={`rounded-lg border px-2.5 py-1.5 text-[12px] disabled:opacity-40 inline-flex items-center gap-1.5 ${tintClass}`}
    >
      {icon} {label}
    </button>
  );
}

function Metric({
  label,
  value,
  icon: Icon,
  danger = false,
}: {
  label: string;
  value: number | string;
  icon: typeof Database;
  danger?: boolean;
}) {
  return (
    <div className={cardClass}>
      <div className="flex items-start justify-between">
        <span className="text-[13px] text-slate-400">{label}</span>
        <Icon className={`h-4 w-4 ${danger ? "text-rose-400" : "text-indigo-400"}`} />
      </div>
      <div className={`mt-3 text-[28px] font-semibold ${danger ? "text-rose-300" : "text-white"}`}>
        {value}
      </div>
    </div>
  );
}

function Value({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return (
    <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3">
      <div className="text-[12px] text-slate-500">{label}</div>
      <div className={`mt-1 text-lg font-semibold ${danger ? "text-rose-300" : "text-slate-200"}`}>
        {value}
      </div>
    </div>
  );
}
