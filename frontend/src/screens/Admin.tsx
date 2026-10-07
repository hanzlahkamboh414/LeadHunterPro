import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity as ActivityIcon,
  AlertTriangle,
  ChevronDown,
  Database,
  Eye,
  EyeOff,
  Gauge,
  KeyRound,
  LockKeyhole,
  LockOpen,
  Mail,
  Pause,
  Phone,
  RefreshCw,
  Share2,
  ShieldCheck,
  Timer,
  Trash2,
  UserPlus,
  Users as UsersIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../contexts/AuthContext";
import { DELETE_REASONS } from "../components/DeleteReasonDialog";
import { Select } from "../components/Select";
import { Spinner } from "../components/StatusChip";
import type {
  AdminDeletedRow,
  AdminLaneMode,
  AdminLaneRepeat,
  AdminLeadScope,
  AdminLeadScopeKind,
  AdminTenant,
  AdminUser,
  AdminVisibilityDate,
} from "../types";

const cardClass =
  "ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5";

const inputClass =
  "w-full rounded-lg border border-white/10 bg-black/20 px-2.5 py-1.5 text-[13px] text-slate-200 placeholder:text-slate-600 focus:border-indigo-400/60 focus:outline-none";

function count(values: Record<string, number>, key: string): string {
  return (values[key] ?? 0).toLocaleString();
}

type Tab = "overview" | "users" | "access" | "data" | "campaigns" | "lanes" | "keys" | "caches" | "audit";

const TABS: { id: Tab; label: string; icon: typeof ShieldCheck }[] = [
  { id: "overview", label: "Overview", icon: Gauge },
  { id: "users", label: "Users & Activity", icon: UsersIcon },
  { id: "access", label: "Admin Access", icon: ShieldCheck },
  { id: "data", label: "Data Control", icon: EyeOff },
  { id: "campaigns", label: "Campaign Sending", icon: Mail },
  { id: "lanes", label: "AI Lanes", icon: Timer },
  { id: "keys", label: "API Keys", icon: KeyRound },
  { id: "caches", label: "Caches", icon: Database },
  { id: "audit", label: "Audit Log", icon: ActivityIcon },
];

export default function Admin({ initialTab = "overview" }: { initialTab?: Tab }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { user: activeUser } = useAuth();
  const isPrimaryAdmin = activeUser?.username === "admin4269" && activeUser.is_admin;
  const allowedTabs = TABS.filter(({ id }) => id === "access" || id === "campaigns"
    ? isPrimaryAdmin : isPrimaryAdmin || activeUser?.admin_permissions.includes(id));
  const [tab, setTab] = useState<Tab>(initialTab);
  useEffect(() => {
    if (allowedTabs.length && !allowedTabs.some(({ id }) => id === tab)) setTab(allowedTabs[0].id);
  }, [activeUser?.admin_permissions, isPrimaryAdmin, tab]);

  const dashboard = useQuery({
    queryKey: ["admin-dashboard"],
    queryFn: () => api.adminDashboard(),
    enabled: allowedTabs.some(({ id }) => id === "overview"),
    refetchInterval: 10_000,
  });
  const keys = useQuery({
    queryKey: ["admin-keys"],
    queryFn: () => api.adminKeys(),
    enabled: allowedTabs.some(({ id }) => id === "keys"),
    refetchInterval: 30_000,
  });
  const deleted = useQuery({
    queryKey: ["admin-deleted"],
    queryFn: () => api.adminDeleted(),
    enabled: allowedTabs.some(({ id }) => id === "audit"),
  });
  const pendingCache = useQuery({
    queryKey: ["admin-pending-cache"],
    queryFn: () => api.adminPendingCache(),
    enabled: allowedTabs.some(({ id }) => id === "caches"),
  });
  const searchCache = useQuery({
    queryKey: ["admin-search-cache"],
    queryFn: () => api.adminSearchCache(),
    enabled: allowedTabs.some(({ id }) => id === "caches"),
  });
  const visibility = useQuery({
    queryKey: ["admin-visibility"],
    queryFn: () => api.adminVisibility(),
    enabled: allowedTabs.some(({ id }) => id === "data"),
    refetchInterval: 10_000,
  });
  const users = useQuery({
    queryKey: ["admin-users"],
    queryFn: () => api.adminUsers(),
    enabled: allowedTabs.some(({ id }) => id === "users" || id === "data" || id === "access"),
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
    if (allowedTabs.some(({ id }) => id === "overview")) dashboard.refetch();
    if (allowedTabs.some(({ id }) => id === "keys")) keys.refetch();
    if (allowedTabs.some(({ id }) => id === "audit")) deleted.refetch();
    if (allowedTabs.some(({ id }) => id === "caches")) { pendingCache.refetch(); searchCache.refetch(); }
    if (allowedTabs.some(({ id }) => id === "data")) visibility.refetch();
    if (allowedTabs.some(({ id }) => id === "users" || id === "data" || id === "access")) users.refetch();
    qc.invalidateQueries({ queryKey: ["admin-activity"] });
  };

  return (
    <div className="workspace-page">
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
        {allowedTabs.map(({ id, label, icon: Icon }) => (
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
      {allowedTabs.length === 0 && <p className="mt-6 text-sm text-slate-400">Loading admin access…</p>}

      {/* -------------------------------------------------------------- */}
      {/* OVERVIEW */}
      {/* -------------------------------------------------------------- */}
      {tab === "overview" && allowedTabs.some(({ id }) => id === tab) && (
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
      {tab === "users" && allowedTabs.some(({ id }) => id === tab) && (
        <UsersTab users={allUsers} usersLoading={users.isLoading} usersError={users.error as Error | null} />
      )}

      {tab === "access" && isPrimaryAdmin && (
        <AdminAccessPanel users={allUsers} loading={users.isLoading} error={users.error as Error | null} />
      )}

      {/* -------------------------------------------------------------- */}
      {/* DATA CONTROL */}
      {/* -------------------------------------------------------------- */}
      {tab === "data" && allowedTabs.some(({ id }) => id === tab) && (
        <>
          <section className={`${cardClass} mt-6`}>
            <h2 className="text-[16px] font-semibold text-white">Campaign lead views</h2>
            <p className="mt-1 text-[12px] text-slate-400">Open Companies with the matching email addresses and campaign counts.</p>
            <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {([
                ["all", "All emails"],
                ["unsent", "In campaigns · not sent"],
                ["sent", "Sent"],
                ["available", "Not in any campaign"],
              ] as const).map(([status, label]) => (
                <button key={status} type="button" onClick={() => navigate(`/leads?folder=*&campaign_status=${status}`)}
                  className="min-h-11 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-left text-[12.5px] font-medium text-slate-200 hover:border-indigo-400/40 hover:bg-indigo-500/10 focus:outline-none focus:ring-2 focus:ring-indigo-400">
                  {label}
                </button>
              ))}
            </div>
          </section>
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
                <Select
                  className="w-56"
                  value={otherScope}
                  onChange={(v) => setOtherScope(v as AdminLeadScopeKind)}
                  options={[
                    { value: "source", label: "By search (trade · location)" },
                    { value: "folder", label: "By folder" },
                    { value: "email", label: "By email" },
                  ]}
                />
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

      {tab === "campaigns" && isPrimaryAdmin && <CampaignSendingPanel />}

      {/* -------------------------------------------------------------- */}
      {/* API KEYS */}
      {/* -------------------------------------------------------------- */}
      {/* -------------------------------------------------------------- */}
      {/* AI LANES                                                       */}
      {/* -------------------------------------------------------------- */}
      {tab === "lanes" && allowedTabs.some(({ id }) => id === tab) && <LaneSchedulePanel />}

      {/* -------------------------------------------------------------- */}
      {/* API KEYS                                                       */}
      {/* -------------------------------------------------------------- */}
      {tab === "keys" && allowedTabs.some(({ id }) => id === tab) && (
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
      {tab === "caches" && allowedTabs.some(({ id }) => id === tab) && (
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
      {/* AUDIT LOG — the user delete-feed + the admin's answers */}
      {/* -------------------------------------------------------------- */}
      {tab === "audit" && allowedTabs.some(({ id }) => id === tab) && (
        <AuditTab
          rows={deleted.data?.deleted ?? []}
          total={deleted.data?.total ?? 0}
          loading={deleted.isLoading}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Primary admin's global sending switch
// ---------------------------------------------------------------------------

function CampaignSendingPanel() {
  const qc = useQueryClient();
  const campaignSending = useQuery({
    queryKey: ["admin-campaign-sending"],
    queryFn: () => api.adminCampaignSendingStatus(),
    refetchInterval: 10_000,
  });
  const pauseCampaignSending = useMutation({
    mutationFn: () => api.adminPauseCampaignSending(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-campaign-sending"] });
      qc.invalidateQueries({ queryKey: ["campaign-sending-status"] });
    },
  });
  const resumeCampaignSending = useMutation({
    mutationFn: () => api.adminResumeCampaignSending(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-campaign-sending"] });
      qc.invalidateQueries({ queryKey: ["campaign-sending-status"] });
    },
  });

  return <section className={`${cardClass} mt-6 max-w-2xl`}>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 className="text-[16px] font-semibold text-white">All campaign sending</h2>
        <p className="mt-1 text-[12.5px] text-slate-400">Master control for every account and campaign.</p>
      </div>
      <span role="status" className={`rounded-full px-3 py-1 text-xs font-semibold ${campaignSending.data?.paused ? "bg-amber-500/15 text-amber-200" : "bg-emerald-500/15 text-emerald-200"}`}>
        {campaignSending.isLoading ? "Checking…" : campaignSending.isError ? "Status unavailable" : campaignSending.data?.paused ? "Sending paused" : "Sending enabled"}
      </span>
    </div>
    <p className="mt-4 max-w-prose text-[13px] leading-relaxed text-slate-300">
      Pause stops new campaign sends while you investigate an issue. Enable lets running or scheduled campaigns continue. Individually paused campaigns and account delivery holds stay separate.
    </p>
    <div className="mt-4 flex flex-wrap gap-2">
      {campaignSending.data?.paused ? (
        <button type="button" onClick={() => resumeCampaignSending.mutate()} disabled={resumeCampaignSending.isPending}
          className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50">
          <LockOpen className="h-4 w-4" /> {resumeCampaignSending.isPending ? "Enabling…" : "Enable sending"}
        </button>
      ) : (
        <button type="button" onClick={() => pauseCampaignSending.mutate()} disabled={!campaignSending.data || pauseCampaignSending.isPending}
          className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-amber-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-amber-500 disabled:cursor-not-allowed disabled:opacity-50">
          <Pause className="h-4 w-4" /> {pauseCampaignSending.isPending ? "Pausing…" : "Pause all sending"}
        </button>
      )}
      <button type="button" onClick={() => void campaignSending.refetch()}
        className="inline-flex min-h-11 items-center gap-2 rounded-lg border border-white/10 px-3 text-[13px] text-slate-200 hover:bg-white/5">
        <RefreshCw className="h-4 w-4" /> Refresh status
      </button>
    </div>
    {(campaignSending.isError || pauseCampaignSending.isError || resumeCampaignSending.isError) && (
      <p role="alert" className="mt-3 text-xs text-rose-300">Could not update campaign sending. Refresh the status and try again.</p>
    )}
    <p className="mt-3 text-xs text-slate-400">An email already in flight may finish after Pause is pressed.</p>
  </section>;
}

// ---------------------------------------------------------------------------
// Audit Log tab — the user delete-feed + the admin's Confirm / Restore answer
// ---------------------------------------------------------------------------

function AuditTab({
  rows,
  total,
  loading,
}: {
  rows: AdminDeletedRow[];
  total: number;
  loading: boolean;
}) {
  const qc = useQueryClient();

  // Confirm: the admin backs the delete — a "not our client" verdict becomes
  // corroborated (decisive identity purge for everyone).
  const confirm = useMutation({
    mutationFn: (email: string) => api.adminConfirmDelete(email),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-deleted"] }),
  });
  // Restore: the admin says the delete was wrong — the lead comes back and
  // the identity learning the delete fed is cleared.
  const restore = useMutation({
    mutationFn: (email: string) => api.adminRestoreDelete(email),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-deleted"] }),
  });

  const reasonLabel = (slug: string): string =>
    DELETE_REASONS.find((r) => r.slug === slug)?.label ?? slug;

  const decisionBadge = (row: AdminDeletedRow): ReactNode => {
    if (row.admin_decision === "confirmed")
      return (
        <span className="rounded-full bg-rose-500/10 px-2 py-0.5 text-[11.5px] text-rose-300">
          Confirmed
        </span>
      );
    if (row.admin_decision === "restored")
      return (
        <span className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11.5px] text-emerald-300">
          Restored
        </span>
      );
    return (
      <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[11.5px] text-amber-300">
        Pending
      </span>
    );
  };

  return (
    <section className={`${cardClass} mt-6`}>
      <h2 className="text-[16px] font-semibold text-white">Deleted leads — user feed</h2>
      <p className="mt-1 text-[12px] text-slate-500">
        Kis user ne kaunsi email kis wajah se delete ki — aur us par aap ka jawab
        (Confirm = delete sahi tha; Restore = lead wapis lao).
      </p>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-left text-[13px]">
          <thead className="text-slate-500">
            <tr>
              <th className="pb-2 pr-4 font-medium">Email</th>
              <th className="pb-2 pr-4 font-medium">User</th>
              <th className="pb-2 pr-4 font-medium">Reason</th>
              <th className="pb-2 pr-4 font-medium">Deleted at</th>
              <th className="pb-2 pr-4 font-medium">Answer</th>
              <th className="pb-2 font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.email} className="border-t border-white/5">
                <td className="py-2 pr-4 text-slate-300">{row.email}</td>
                <td className="py-2 pr-4 text-slate-300">{row.username}</td>
                <td className="py-2 pr-4">
                  <span className={`rounded-full px-2 py-0.5 text-[11.5px] ${
                    row.reason === "not_our_client"
                      ? "bg-rose-500/10 text-rose-300"
                      : "bg-slate-500/10 text-slate-400"
                  }`}>
                    {reasonLabel(row.reason)}
                  </span>
                </td>
                <td className="py-2 pr-4 text-slate-400">{row.deleted_at}</td>
                <td className="py-2 pr-4">{decisionBadge(row)}</td>
                <td className="py-2">
                  <div className="flex items-center gap-2">
                    {row.admin_decision === "" && (
                      <button
                        type="button"
                        disabled={confirm.isPending}
                        onClick={() => confirm.mutate(row.email)}
                        className="rounded-md border border-rose-400/30 bg-rose-500/10 px-2.5 py-1 text-[12px] text-rose-300 hover:bg-rose-500/20 disabled:opacity-50"
                      >
                        {confirm.isPending ? <Spinner /> : "Confirm"}
                      </button>
                    )}
                    {row.admin_decision !== "restored" && (
                      <button
                        type="button"
                        disabled={restore.isPending}
                        onClick={() => restore.mutate(row.email)}
                        className="rounded-md border border-emerald-400/30 bg-emerald-500/10 px-2.5 py-1 text-[12px] text-emerald-300 hover:bg-emerald-500/20 disabled:opacity-50"
                      >
                        {restore.isPending ? <Spinner /> : "Restore"}
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {rows.length === 0 && !loading && (
              <tr><td colSpan={6} className="py-4 text-slate-500">Nothing deleted yet.</td></tr>
            )}
          </tbody>
        </table>
        {loading && (
          <p className="mt-3 text-[12px] text-slate-500"><Spinner /> Loading…</p>
        )}
        <p className="mt-3 text-[11px] text-slate-600">{total} total deleted</p>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Users & Activity tab
// ---------------------------------------------------------------------------

function AdminAccessPanel({ users, loading, error }: {
  users: AdminUser[];
  loading: boolean;
  error: Error | null;
}) {
  const qc = useQueryClient();
  const manageable = users.filter((user) => user.username !== "admin4269" && user.username !== "shared");
  const [selectedId, setSelectedId] = useState("");
  const selected = manageable.find((user) => user.id === selectedId) ?? manageable[0];
  const [draft, setDraft] = useState<string[]>([]);
  const [savedName, setSavedName] = useState("");
  useEffect(() => {
    setDraft(selected?.admin_permissions ?? []);
    setSavedName("");
  }, [selected?.id, selected?.admin_permissions]);
  const save = useMutation({
    mutationFn: () => api.adminSetUserPermissions(selected!.id, draft),
    onSuccess: (updated) => {
      setSavedName(updated.username);
      qc.invalidateQueries({ queryKey: ["admin-users"] });
      qc.invalidateQueries({ queryKey: ["admin-activity"] });
    },
  });
  const controls = TABS.filter(({ id }) => id !== "access" && id !== "campaigns");
  const unchanged = JSON.stringify([...draft].sort()) ===
    JSON.stringify([...(selected?.admin_permissions ?? [])].sort());

  return <section className={`${cardClass} mt-6`} aria-label="Admin permissions">
    <div className="flex items-center gap-2">
      <ShieldCheck className="h-5 w-5 text-teal-300" />
      <h2 className="text-lg font-semibold text-white">Admin permissions</h2>
    </div>
    <p className="mt-1 text-[13px] text-slate-400">
      Choose an account, then grant only the admin sections it should manage. Clearing every control removes admin access.
    </p>
    {loading && <p className="mt-4 text-sm text-slate-400"><Spinner /> Loading accounts…</p>}
    {error && <p role="alert" className="mt-4 text-sm text-rose-300">Accounts unavailable: {error.message}</p>}
    {!loading && !error && manageable.length === 0 &&
      <p className="mt-4 text-sm text-slate-400">Create a user account first, then grant admin access here.</p>}
    {selected && <>
      <label className="mt-5 block max-w-lg text-[12px] font-medium text-slate-300" htmlFor="admin-access-user">
        Account
        <select id="admin-access-user" value={selected.id} onChange={(event) => setSelectedId(event.target.value)}
          className="mt-1 min-h-11 w-full rounded-lg border border-white/10 bg-[#151923] px-3 text-[13px] text-slate-100">
          {manageable.map((user) => <option key={user.id} value={user.id}>
            {user.name || user.username} ({user.username}) {user.is_admin ? "· admin" : "· user"}
          </option>)}
        </select>
      </label>
      <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {controls.map(({ id, label, icon: Icon }) => <label key={id}
          className={`flex min-h-12 cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 text-[13px] ${draft.includes(id)
            ? "border-teal-500/40 bg-teal-500/10 text-white" : "border-white/10 bg-white/[0.02] text-slate-300"}`}>
          <input type="checkbox" checked={draft.includes(id)}
            onChange={() => setDraft((current) => current.includes(id)
              ? current.filter((value) => value !== id) : [...current, id])}
            className="h-4 w-4 accent-teal-500" />
          <Icon className="h-4 w-4 shrink-0" />{label}
        </label>)}
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button type="button" onClick={() => setDraft(controls.map(({ id }) => id))}
          className="rounded-lg border border-white/10 px-3 py-2 text-xs text-slate-200 hover:bg-white/[0.05]">Select all</button>
        <button type="button" onClick={() => setDraft([])}
          className="rounded-lg border border-white/10 px-3 py-2 text-xs text-slate-200 hover:bg-white/[0.05]">Remove all</button>
        <button type="button" disabled={save.isPending || unchanged} onClick={() => save.mutate()}
          className="min-h-10 rounded-lg bg-teal-700 px-4 py-2 text-[13px] font-semibold text-white hover:bg-teal-600 disabled:opacity-50">
          {save.isPending ? "Saving…" : "Save permissions"}
        </button>
      </div>
      {savedName && <p role="status" className="mt-3 text-[12px] text-teal-300">Permissions saved for {savedName}.</p>}
      {save.isError && <p role="alert" className="mt-3 text-[12px] text-rose-300">{(save.error as Error).message}</p>}
    </>}
  </section>;
}

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
  const { user: activeUser, tenants: ownTenants } = useAuth();
  const isPlatformAdmin = Boolean(activeUser?.is_platform_admin);
  const isPrimaryAdmin = activeUser?.username === "admin4269" && activeUser.is_admin;
  const [expanded, setExpanded] = useState<string | null>(null);
  const [activityFilter, setActivityFilter] = useState<string>("");
  const [tenantName, setTenantName] = useState("");
  const [selectedTenant, setSelectedTenant] = useState("");
  const [selectedPhoneUserId, setSelectedPhoneUserId] = useState("");
  const [phoneLimitDraft, setPhoneLimitDraft] = useState("");
  const [phoneLimitSaved, setPhoneLimitSaved] = useState("");
  const selectedPhoneUser = users.find((u) => u.id === selectedPhoneUserId && u.username !== "admin4269" && u.username !== "shared")
    ?? users.find((u) => u.username !== "admin4269" && u.username !== "shared");
  const validPhoneLimit = /^\d+$/.test(phoneLimitDraft) && Number(phoneLimitDraft) <= 5000;
  useEffect(() => {
    if (selectedPhoneUser) setPhoneLimitDraft(String(selectedPhoneUser.phone_daily_limit));
  }, [selectedPhoneUser?.id, selectedPhoneUser?.phone_daily_limit]);

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
    mutationFn: (body: { username: string; email: string; password: string; name?: string; tenant_id?: string; role?: "owner" | "member" }) =>
      api.adminCreateUser(body),
    onSuccess: () => {
      invalidateUsers();
      qc.invalidateQueries({ queryKey: ["admin-tenants"] });
      qc.invalidateQueries({ queryKey: ["admin-tenant-members"] });
    },
  });
  const deleteUser = useMutation({
    mutationFn: (userId: string) => api.adminDeleteUser(userId),
    onSuccess: invalidateUsers,
  });
  const resetPassword = useMutation({
    mutationFn: ({ userId, password }: { userId: string; password: string }) =>
      api.adminResetUserPassword(userId, password),
  });
  const setPhoneLimit = useMutation({
    mutationFn: ({ userId, limit }: { userId: string; limit: number }) =>
      api.adminSetPhoneLimit(userId, limit),
    onSuccess: (result) => {
      invalidateUsers();
      setPhoneLimitSaved(`Saved ${result.daily_limit} numbers/day for ${
        users.find((u) => u.id === result.user_id)?.username ?? "user"
      }.`);
    },
  });
  const setImportLimit = useMutation({
    mutationFn: ({ userId, limit }: { userId: string; limit: number }) =>
      api.adminSetImportLimit(userId, limit),
    onSuccess: invalidateUsers,
  });

  // Login-auth ON/OFF — the open-site switch. OFF means: no login page,
  // visitors share one account, and THIS panel is reachable only through the
  // secret /admin4269 password gate.
  const authMode = useQuery({
    queryKey: ["auth-mode"],
    queryFn: () => api.authMode(),
  });
  const tenantMode = authMode.data?.tenant_mode === true;
  const tenants = useQuery({
    queryKey: ["admin-tenants"],
    queryFn: () => api.adminTenants(),
    enabled: tenantMode && isPlatformAdmin,
  });
  const members = useQuery({
    queryKey: ["admin-tenant-members", selectedTenant],
    queryFn: () => api.adminTenantMembers(selectedTenant),
    enabled: tenantMode && isPlatformAdmin && !!selectedTenant,
  });
  const createTenant = useMutation({
    mutationFn: (name: string) => api.adminCreateTenant(name),
    onSuccess: (created) => {
      setTenantName("");
      setSelectedTenant(created.id);
      qc.invalidateQueries({ queryKey: ["admin-tenants"] });
    },
  });
  const addTenantMember = useMutation({
    mutationFn: ({ tenantId, userId }: { tenantId: string; userId: string }) =>
      api.adminAddTenantMember(tenantId, userId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-tenants"] });
      qc.invalidateQueries({ queryKey: ["admin-tenant-members", selectedTenant] });
    },
  });
  const removeTenantMember = useMutation({
    mutationFn: ({ tenantId, userId }: { tenantId: string; userId: string }) =>
      api.adminRemoveTenantMember(tenantId, userId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-tenants"] });
      qc.invalidateQueries({ queryKey: ["admin-tenant-members", selectedTenant] });
    },
  });
  const toggleAuth = useMutation({
    mutationFn: (enabled: boolean) => api.adminSetAuthMode(enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth-mode"] }),
  });

  // Gmail-inbox interface ON/OFF — the Gmail-app-like browse/read/send screen.
  // OFF means: Email screen shows only the address XLSX export (which always
  // works); ON brings the full inbox interface back.
  const gmailMode = useQuery({
    queryKey: ["gmail-mode"],
    queryFn: () => api.gmailMode(),
  });
  const toggleGmailInbox = useMutation({
    mutationFn: (enabled: boolean) => api.adminSetGmailInboxMode(enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["gmail-mode"] }),
  });
  const claims = useQuery({
    queryKey: ["admin-phone-claims"],
    queryFn: () => api.adminPhoneClaimsReport(),
  });
  const [wrongArchiveOpen, setWrongArchiveOpen] = useState(false);
  const [wrongArchivePage, setWrongArchivePage] = useState(0);
  const wrongArchivePageSize = 10;
  const wrongPhones = useQuery({
    queryKey: ["admin-wrong-phones", wrongArchivePage],
    queryFn: () => api.adminWrongPhonesPage(wrongArchivePageSize, wrongArchivePage * wrongArchivePageSize),
    enabled: wrongArchiveOpen,
  });
  const recoverWrong = useMutation({
    mutationFn: (id: number) => api.adminRecoverWrongPhone(id),
    onSuccess: () => {
      setWrongArchivePage(0);
      qc.invalidateQueries({ queryKey: ["admin-wrong-phones"] });
    },
  });

  function flipAuth() {
    const currentlyOn = authMode.data?.auth_enabled ?? true;
    if (currentlyOn) {
      const ok = window.confirm(
        "Login page OFF kar dein?\n\n" +
          "Iske baad:\n" +
          "• Site khud normal user UI me khulegi (koi login nahi)\n" +
          "• Sab visitors ek shared account par kaam karenge\n" +
          "• Admin panel SIRF leadhuntarpro.online/admin4269 se milega (password ke baad)\n\n" +
          "Continue?"
      );
      if (!ok) return;
      toggleAuth.mutate(false);
    } else {
      toggleAuth.mutate(true);
    }
  }

  return (
    <>
      {/* Login auth toggle */}
      <section className={`${cardClass} mt-6`}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            {(authMode.data?.auth_enabled ?? true) ? (
              <LockKeyhole className="h-4 w-4 text-emerald-400" />
            ) : (
              <LockOpen className="h-4 w-4 text-amber-400" />
            )}
            <h2 className="text-[16px] font-semibold text-white">Login page</h2>
            <span
              className={`rounded-full px-2 py-0.5 text-[11.5px] font-medium ${
                (authMode.data?.auth_enabled ?? true)
                  ? "bg-emerald-500/10 text-emerald-300"
                  : "bg-amber-500/10 text-amber-300"
              }`}
            >
              {(authMode.data?.auth_enabled ?? true) ? "ON — login required" : "OFF — open site"}
            </span>
          </div>
          <button
            onClick={flipAuth}
            disabled={tenantMode || toggleAuth.isPending || authMode.isLoading}
            className={`rounded-lg px-3.5 py-2 text-[12.5px] font-medium disabled:opacity-50 ${
              (authMode.data?.auth_enabled ?? true)
                ? "border border-amber-500/40 text-amber-300 hover:bg-amber-500/10"
                : "bg-emerald-600 text-white hover:bg-emerald-500"
            }`}
          >
            {toggleAuth.isPending
              ? "Saving…"
              : (authMode.data?.auth_enabled ?? true)
                ? "Turn OFF (open site)"
                : "Turn ON (login required)"}
          </button>
        </div>
        <p className="mt-1 text-[12px] text-slate-500">
          {tenantMode
            ? "Tenant mode mein login hamesha ON rahega."
            : (authMode.data?.auth_enabled ?? true)
            ? "Har visitor ko login karna zaroori hai. OFF karne par site seedha normal user UI me khulegi aur admin panel sirf /admin4269 password gate se milega."
            : "Site open hai — sab visitors shared account par kaam rahe hain. Admin panel ke liye leadhuntarpro.online/admin4269 par admin password chahiye."}
        </p>
        {toggleAuth.isError && (
          <p className="mt-2 text-[12px] text-rose-300">
            Toggle failed: {(toggleAuth.error as Error).message}
          </p>
        )}
      </section>

      {/* Gmail inbox interface toggle */}
      <section className={`${cardClass} mt-6`}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <Mail className="h-4 w-4 text-emerald-400" />
            <h2 className="text-[16px] font-semibold text-white">Gmail inbox interface</h2>
            <span
              className={`rounded-full px-2 py-0.5 text-[11.5px] font-medium ${
                (gmailMode.data?.inbox_enabled ?? true)
                  ? "bg-emerald-500/10 text-emerald-300"
                  : "bg-amber-500/10 text-amber-300"
              }`}
            >
              {(gmailMode.data?.inbox_enabled ?? true)
                ? "ON — full inbox"
                : "OFF — address export only"}
            </span>
          </div>
          <button
            onClick={() => toggleGmailInbox.mutate(!(gmailMode.data?.inbox_enabled ?? true))}
            disabled={toggleGmailInbox.isPending || gmailMode.isLoading}
            className={`rounded-lg px-3.5 py-2 text-[12.5px] font-medium disabled:opacity-50 ${
              (gmailMode.data?.inbox_enabled ?? true)
                ? "border border-amber-500/40 text-amber-300 hover:bg-amber-500/10"
                : "bg-emerald-600 text-white hover:bg-emerald-500"
            }`}
          >
            {toggleGmailInbox.isPending
              ? "Saving…"
              : (gmailMode.data?.inbox_enabled ?? true)
                ? "Turn OFF (export only)"
                : "Turn ON (full inbox)"}
          </button>
        </div>
        <p className="mt-1 text-[12px] text-slate-500">
          {(gmailMode.data?.inbox_enabled ?? true)
            ? "Email screen par poora Gmail interface hai — inbox, sent, padhna, reply, compose. OFF karne par sirf email addresses ki XLSX export bachegi (jo hamesha chalti rehti hai)."
            : "Gmail interface OFF hai — Email screen par sirf email addresses ki XLSX export available hai. ON karne par poora inbox interface wapas aa jayega."}
        </p>
        {toggleGmailInbox.isError && (
          <p className="mt-2 text-[12px] text-rose-300">
            Toggle failed: {(toggleGmailInbox.error as Error).message}
          </p>
        )}
      </section>

      {/* Phone call sheets — the hidden-stock report */}
      <section className={`${cardClass} mt-6`}>
        <div className="flex items-center gap-2">
          <svg className="h-4 w-4 text-cyan-400" viewBox="0 0 20 20" fill="currentColor">
            <path d="M2 3a1 1 0 011-1h2.153a1 1 0 01.986.836l.74 4.435a1 1 0 01-.54 1.06l-1.548.773a11.037 11.037 0 006.105 6.105l.774-1.548a1 1 0 011.059-.54l4.435.74a1 1 0 01.836.986V17a1 1 0 01-1 1h-2C7.82 18 2 12.18 2 5V3z" />
          </svg>
          <h2 className="text-[16px] font-semibold text-white">Phone call sheets</h2>
        </div>
        <p className="mt-1 text-[12px] text-slate-500">
          A user's sheet shows today's still-owned numbers across all states. Older claims stay
          exclusive and are counted as hidden here; their call history remains available by date.
        </p>
        {claims.isLoading ? (
          <p className="mt-3 text-[12px] text-slate-500">Loading…</p>
        ) : claims.isError ? (
          <p className="mt-3 text-[12px] text-rose-400">Failed to load: {(claims.error as Error).message}</p>
        ) : claims.data && (
          <>
            <p className="mt-2 text-[12.5px] text-slate-400">
              <b className="text-white">{claims.data.total_claims}</b> total claims ·{" "}
              <b className="text-amber-400">{claims.data.total_hidden}</b> hidden
            </p>
            <table className="mt-3 w-full text-left text-[12.5px]">
              <thead>
                <tr className="text-slate-500">
                  <th className="pb-1 pr-3 font-medium">User</th>
                  <th className="pb-1 pr-3 font-medium text-right">Sheet</th>
                  <th className="pb-1 pr-3 font-medium text-right">Hidden</th>
                  <th className="pb-1 font-medium text-right">Last claimed</th>
                </tr>
              </thead>
              <tbody>
                {claims.data.by_user.map((row) => (
                  <tr key={row.user_id} className="border-t border-slate-800/60">
                    <td className="py-1.5 pr-3 text-white">{row.username || row.user_id.slice(0, 8)}</td>
                    <td className="py-1.5 pr-3 text-right text-slate-300">{row.visible}</td>
                    <td className="py-1.5 pr-3 text-right text-amber-400">{row.hidden}</td>
                    <td className="py-1.5 pr-3 text-right text-slate-500">{row.last_claimed.slice(0, 10) || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )}
      </section>

      <section className={`${cardClass} mt-6`}>
        <button type="button" onClick={() => setWrongArchiveOpen((open) => !open)} aria-expanded={wrongArchiveOpen} aria-controls="admin-wrong-archive"
          className="flex min-h-11 w-full items-center justify-between gap-3 text-left text-[16px] font-semibold text-white">
          <span>Invalid-number archive {wrongPhones.data && <span className="ml-1 text-xs font-medium text-slate-300">({wrongPhones.data.total.toLocaleString()})</span>}</span>
          <ChevronDown className={`h-4 w-4 shrink-0 text-slate-300 transition-transform ${wrongArchiveOpen ? "rotate-180" : ""}`} aria-hidden="true" />
        </button>
        {wrongArchiveOpen && <div id="admin-wrong-archive" className="border-t border-white/10 pt-3">
          <p className="text-[12px] text-slate-400">Removed from caller sheets. Recover a record here when needed.</p>
          {wrongPhones.isLoading && <p className="mt-2 text-xs text-slate-400">Loading archive…</p>}
          {wrongPhones.isError && <p role="alert" className="mt-2 text-xs text-rose-300">Archive unavailable: {(wrongPhones.error as Error).message} <button type="button" className="underline" onClick={() => void wrongPhones.refetch()}>Retry</button></p>}
          {(wrongPhones.data?.rows ?? []).map((row) => <div key={row.id} className="flex flex-wrap items-center justify-between gap-2 border-b border-white/5 py-2 text-xs">
            <span className="min-w-0 break-all text-slate-200">{row.phone} · {row.created_at.slice(0, 10)} · {row.user_id} <span className="ml-1 rounded bg-white/10 px-1.5 py-0.5 text-slate-300">{row.reason === "no_longer_in_service" ? "No longer in service" : row.reason === "not_exist" ? "Not exist" : "Wrong number"}</span></span>
            <button type="button" disabled={recoverWrong.isPending} onClick={() => recoverWrong.mutate(row.id)}
              className="min-h-10 shrink-0 rounded-md bg-indigo-500/15 px-3 text-indigo-200 disabled:opacity-50">Recover</button>
          </div>)}
          {wrongPhones.data?.total === 0 && <p className="mt-2 text-xs text-slate-400">No invalid numbers in archive.</p>}
          {wrongPhones.data && wrongPhones.data.total > wrongArchivePageSize && <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-300">
            <span>{wrongArchivePage * wrongArchivePageSize + 1}–{Math.min((wrongArchivePage + 1) * wrongArchivePageSize, wrongPhones.data.total)} of {wrongPhones.data.total}</span>
            <div className="flex gap-2">
              <button type="button" disabled={wrongArchivePage === 0 || wrongPhones.isFetching} onClick={() => setWrongArchivePage((page) => page - 1)} className="min-h-10 rounded-lg border border-white/15 px-3 disabled:opacity-40">Previous</button>
              <button type="button" disabled={(wrongArchivePage + 1) * wrongArchivePageSize >= wrongPhones.data.total || wrongPhones.isFetching} onClick={() => setWrongArchivePage((page) => page + 1)} className="min-h-10 rounded-lg border border-white/15 px-3 disabled:opacity-40">Next</button>
            </div>
          </div>}
          {recoverWrong.isError && <p role="alert" className="mt-2 text-xs text-rose-300">Recovery failed: {(recoverWrong.error as Error).message}</p>}
        </div>}
      </section>

      {tenantMode && isPlatformAdmin && (
        <section className={`${cardClass} mt-6`}>
          <div className="flex items-center gap-2">
            <UsersIcon className="h-4 w-4 text-indigo-400" />
            <h2 className="text-[16px] font-semibold text-white">Tenants</h2>
          </div>
          <p className="mt-1 text-[12px] text-slate-500">
            Har company ka alag workspace. Naya tenant banane ke baad us ka pehla owner naya account hoga.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <input
              value={tenantName}
              onChange={(e) => setTenantName(e.target.value)}
              placeholder="Company / tenant name"
              className={`${inputClass} max-w-sm`}
            />
            <button
              type="button"
              disabled={!tenantName.trim() || createTenant.isPending}
              onClick={() => createTenant.mutate(tenantName.trim())}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white disabled:opacity-50"
            >
              Create tenant
            </button>
          </div>
          {createTenant.isError && <p className="mt-2 text-xs text-rose-300">{(createTenant.error as Error).message}</p>}
          {tenants.isError && <p className="mt-2 text-xs text-rose-300">Tenants unavailable: {(tenants.error as Error).message}</p>}
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <Select
              className="w-64"
              value={selectedTenant}
              onChange={setSelectedTenant}
              placeholder="Select tenant"
              options={(tenants.data ?? []).map((tenant) => ({
                value: tenant.id,
                label: `${tenant.name} (${tenant.member_count} users)`,
              }))}
            />
            <span className="text-xs text-slate-500">
              {(tenants.data ?? []).length} tenant(s) total
            </span>
          </div>
          {selectedTenant && (
            <div className="mt-4">
              <p className="text-[13px] font-medium text-slate-200">
                {(tenants.data ?? []).find((tenant) => tenant.id === selectedTenant)?.name ?? "Tenant"} members
              </p>
              {members.isLoading && <p className="mt-2 text-xs text-slate-500">Loading members…</p>}
              {members.isError && <p className="mt-2 text-xs text-rose-300">Members unavailable: {(members.error as Error).message}</p>}
              {(members.data ?? []).map((member) => (
                <div key={member.user_id} className="flex items-center justify-between border-b border-white/5 py-2 text-xs">
                  <span className="text-slate-300">{member.username} · {member.role}{member.status === "disabled" ? " · disabled" : ""}</span>
                  {member.status === "disabled" ? (
                    <button
                      type="button"
                      disabled={addTenantMember.isPending}
                      onClick={() => addTenantMember.mutate({ tenantId: selectedTenant, userId: member.user_id })}
                      className="text-emerald-300 hover:text-emerald-200 disabled:opacity-50"
                    >Reactivate</button>
                  ) : member.role === "member" && (
                    <button
                      type="button"
                      disabled={removeTenantMember.isPending}
                      onClick={() => {
                        if (window.confirm(`Disable ${member.username}'s account? They will lose access immediately.`)) {
                          removeTenantMember.mutate({ tenantId: selectedTenant, userId: member.user_id });
                        }
                      }}
                      className="text-rose-300 hover:text-rose-200 disabled:opacity-50"
                    >Disable</button>
                  )}
                </div>
              ))}
              {addTenantMember.isError && <p className="mt-2 text-xs text-rose-300">{(addTenantMember.error as Error).message}</p>}
              {removeTenantMember.isError && <p className="mt-2 text-xs text-rose-300">{(removeTenantMember.error as Error).message}</p>}
            </div>
          )}
        </section>
      )}

      {/* Phone quota control stays visible instead of being hidden in a user row. */}
      <section className={`${cardClass} mt-6`} aria-label="Phone number daily limits">
        <div className="flex items-center gap-2">
          <Phone className="h-4 w-4 text-teal-400" />
          <h2 className="text-[16px] font-semibold text-white">Phone number limits</h2>
        </div>
        <p className="mt-1 text-[12px] text-slate-400">
          Default: 600 numbers per user per UTC day. Set any limit from 0 to 5000.
        </p>
        {selectedPhoneUser ? (
          <div className="mt-4 flex flex-wrap items-end gap-3">
            <label className="flex min-w-[180px] flex-1 flex-col gap-1 text-[12px] text-slate-300">
              Account
              <select
                value={selectedPhoneUser.id}
                onChange={(event) => { setSelectedPhoneUserId(event.target.value); setPhoneLimitSaved(""); }}
                className="min-h-11 rounded-lg border border-white/10 bg-[#151923] px-3 text-[13px] text-slate-100"
              >
                {users.filter((u) => u.username !== "admin4269" && u.username !== "shared").map((u) => (
                  <option key={u.id} value={u.id}>{u.name || u.username} ({u.username})</option>
                ))}
              </select>
            </label>
            <label className="flex w-36 flex-col gap-1 text-[12px] text-slate-300">
              Numbers per day
              <input
                type="number" min={0} max={5000} step={1}
                value={phoneLimitDraft}
                onChange={(event) => { setPhoneLimitDraft(event.target.value); setPhoneLimitSaved(""); }}
                className="min-h-11 rounded-lg border border-white/10 bg-[#151923] px-3 text-[13px] text-slate-100"
              />
            </label>
            <button
              type="button"
              disabled={setPhoneLimit.isPending || !validPhoneLimit || Number(phoneLimitDraft) === selectedPhoneUser.phone_daily_limit}
              onClick={() => setPhoneLimit.mutate({ userId: selectedPhoneUser.id, limit: Number(phoneLimitDraft) })}
              className="min-h-11 rounded-lg bg-teal-700 px-4 text-[13px] font-semibold text-white hover:bg-teal-600 disabled:opacity-50"
            >
              {setPhoneLimit.isPending ? "Saving…" : "Save limit"}
            </button>
            <span className="pb-3 text-[12px] text-slate-400">
              {selectedPhoneUser.phone_daily_used} used today
            </span>
          </div>
        ) : (
          <p className="mt-3 text-[12px] text-slate-400">Create a user account to set its phone limit.</p>
        )}
        {phoneLimitSaved && <p role="status" className="mt-2 text-[12px] text-teal-300">{phoneLimitSaved}</p>}
        {setPhoneLimit.isError && <p role="alert" className="mt-2 text-[12px] text-rose-300">{(setPhoneLimit.error as Error).message}</p>}
      </section>

      {/* Create account */}
      <section className={`${cardClass} mt-6`}>
        <div className="flex items-center gap-2">
          <UserPlus className="h-4 w-4 text-emerald-400" />
          <h2 className="text-[16px] font-semibold text-white">Add an account</h2>
        </div>
        <p className="mt-1 text-[12px] text-slate-500">
          Create a user account directly — the user logs in with this username + password.
        </p>
        <CreateUserForm
          busy={createUser.isPending}
          error={createUser.error as Error | null}
          tenants={tenantMode ? (isPlatformAdmin ? tenants.data ?? [] : ownTenants.map(
            (tenant) => ({ id: tenant.id, name: tenant.name, member_count: 1 }),
          )) : undefined}
          onCreate={(b) => createUser.mutate(b)}
        />
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
          delete accounts here. Use the Admin Access tab to grant admin controls.
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
                <th className="pb-2 pr-4 font-medium">Name</th>
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
                  onSetPhoneLimit={(limit) => setPhoneLimit.mutate({ userId: u.id, limit })}
                  phoneLimitPending={setPhoneLimit.isPending}
                  isPrimaryAdmin={Boolean(isPrimaryAdmin)}
                  onSetImportLimit={(limit) => setImportLimit.mutate({ userId: u.id, limit })}
                  importLimitPending={setImportLimit.isPending}
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
        {setImportLimit.isError && <p role="alert" className="mt-3 text-[12px] text-rose-300">{(setImportLimit.error as Error).message}</p>}
      </section>

      {/* Activity log */}
      <section className={`${cardClass} mt-5`}>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <ActivityIcon className="h-4 w-4 text-sky-400" />
            <h2 className="text-[16px] font-semibold text-white">Activity log</h2>
          </div>
          <Select
            className="w-44"
            value={activityFilter}
            onChange={setActivityFilter}
            placeholder="All users"
            options={users.map((u) => ({ value: u.id, label: u.username }))}
          />
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
  tenants,
  onCreate,
}: {
  busy: boolean;
  error: Error | null;
  tenants?: AdminTenant[];
  onCreate: (body: { username: string; email: string; password: string; name?: string; tenant_id?: string; role?: "owner" | "member" }) => void;
}) {
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [tenantId, setTenantId] = useState("");
  return (
    <div className="mt-3">
      {tenants && (
        <div className="mb-3">
          <Select
            className="w-72"
            value={tenantId}
            onChange={setTenantId}
            placeholder="Choose tenant for this account"
            options={tenants.map((tenant) => ({ value: tenant.id, label: tenant.name }))}
          />
        </div>
      )}
      {tenants?.find((tenant) => tenant.id === tenantId)?.member_count === 0 && (
        <p className="mb-2 text-xs text-amber-300">This first account will own the company.</p>
      )}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-4">
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="username"
          className={inputClass}
        />
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="full name (shown in their topbar)"
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
            onCreate({
              username: username.trim(), email: email.trim(), password,
              name: name.trim(), ...(tenants ? {
                tenant_id: tenantId,
                role: tenants.find((tenant) => tenant.id === tenantId)?.member_count === 0
                  ? "owner" as const : "member" as const,
              } : {}),
            });
            setUsername("");
            setName("");
            setEmail("");
            setPassword("");
          }}
          disabled={busy || !username.trim() || !email.trim() || !password || !name.trim() || (tenants !== undefined && !tenantId)}
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
  onSetPhoneLimit,
  phoneLimitPending,
  isPrimaryAdmin,
  onSetImportLimit,
  importLimitPending,
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
  onSetPhoneLimit: (limit: number) => void;
  phoneLimitPending: boolean;
  isPrimaryAdmin: boolean;
  onSetImportLimit: (limit: number) => void;
  importLimitPending: boolean;
}) {
  const [phoneLimit, setPhoneLimit] = useState(user.phone_daily_limit);
  const [importLimit, setImportLimit] = useState(user.import_daily_limit ?? 100);
  useEffect(() => { setPhoneLimit(user.phone_daily_limit); }, [user.phone_daily_limit]);
  useEffect(() => { setImportLimit(user.import_daily_limit ?? 100); }, [user.import_daily_limit]);
  return (
    <>
      <tr className={`border-t border-white/5 ${expanded ? "bg-white/[0.02]" : "hover:bg-white/[0.02]"}`}>
        <td className="py-2.5 pr-4">
          <button onClick={onToggle} className="text-left hover:text-white">
            <span className={`inline-block transition-transform mr-1.5 text-slate-500 ${expanded ? "rotate-90" : ""}`}>›</span>
            <span className="text-slate-200 font-medium">{user.name || user.username}</span>
            {user.name && user.name !== user.username && (
              <span className="ml-2 text-[11.5px] text-slate-500">@{user.username}</span>
            )}
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
            {user.username !== "admin4269" && user.username !== "shared" && <div className="flex flex-wrap items-center gap-2 mb-3 text-[12px] text-slate-300">
              <label htmlFor={`phone-limit-${user.id}`}>Phone numbers per day (UTC)</label>
              <input id={`phone-limit-${user.id}`} type="number" min={0} max={5000}
                value={phoneLimit} onChange={(e) => setPhoneLimit(Number(e.target.value))}
                className="w-24 rounded-md border border-white/10 bg-white/[0.04] px-2 py-1 text-white" />
              <button type="button" disabled={phoneLimitPending || !Number.isInteger(phoneLimit) || phoneLimit < 0 || phoneLimit > 5000}
                onClick={() => onSetPhoneLimit(phoneLimit)}
                className="rounded-md bg-indigo-500/20 px-2.5 py-1 text-indigo-200 disabled:opacity-50">Save limit</button>
              <span className="text-slate-500">{user.phone_daily_used} used today</span>
            </div>}
            {isPrimaryAdmin && user.username !== "admin4269" && user.username !== "shared" && <div className="mb-3 flex flex-wrap items-center gap-2 text-[12px] text-slate-300">
              <label htmlFor={`import-limit-${user.id}`}>Manual research per day</label>
              <input id={`import-limit-${user.id}`} type="number" min={0} max={1000000} value={importLimit}
                onChange={(event) => setImportLimit(Number(event.target.value))}
                className="w-28 rounded-md border border-white/10 bg-white/[0.04] px-2 py-1 text-white" />
              <button type="button" disabled={importLimitPending || !Number.isInteger(importLimit) || importLimit < 0 || importLimit > 1000000 || importLimit === user.import_daily_limit}
                onClick={() => onSetImportLimit(importLimit)}
                className="rounded-md bg-indigo-500/20 px-2.5 py-1 text-indigo-200 disabled:opacity-50">Save research limit</button>
              <span className="text-slate-400">{user.import_daily_used ?? 0} used today</span>
            </div>}
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
        <Select
          className="w-52"
          value={scope}
          onChange={(v) => setScope(v as AdminLeadScopeKind)}
          options={[
            { value: "folder", label: "By folder" },
            { value: "date", label: "By date" },
            { value: "source", label: "By search (trade · location)" },
            { value: "email", label: "By email" },
          ]}
        />
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={scope === "email" ? "owner@company.com" : scope === "folder" ? 'e.g. Q3 Outreach' : scope === "date" ? "YYYY-MM-DD" : 'e.g. GC · Houston, TX'}
          className={inputClass + " w-64"}
        />
        <Select
          className="w-40"
          value={userId}
          onChange={setUserId}
          placeholder="Select user…"
          options={nonAdminUsers.map((u) => ({ value: u.id, label: u.username }))}
        />
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

function LaneSchedulePanel() {
  const qc = useQueryClient();
  const lane = useQuery({
    queryKey: ["admin-lane"],
    queryFn: () => api.adminLaneStatus(),
    refetchInterval: 10_000,
  });
  const save = useMutation({
    mutationFn: (body: {
      mode: AdminLaneMode;
      phone_min?: number;
      email_min?: number;
      phone_first?: boolean;
      repeat?: AdminLaneRepeat;
    }) => api.adminSetLaneSchedule(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-lane"] });
      qc.invalidateQueries({ queryKey: ["admin-activity"] });
    },
  });
  const control = useMutation({
    mutationFn: (mode: "off" | "on" | "schedule") => api.adminSetHarvesterControl(mode),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-lane"] });
      qc.invalidateQueries({ queryKey: ["admin-activity"] });
    },
  });

  // Local draft — seeded from the server the first time it answers, then
  // owned by the operator. NOT re-seeded on every 10s poll, or a half-typed
  // "40" would be overwritten by the still-saved "90" mid-edit.
  const [draft, setDraft] = useState<{
    mode: AdminLaneMode;
    phone_min: number;
    email_min: number;
    phone_first: boolean;
    repeat: AdminLaneRepeat;
  } | null>(null);
  const seeded = lane.data;
  if (draft === null && seeded) {
    setDraft({
      mode: seeded.mode,
      phone_min: seeded.phone_min,
      email_min: seeded.email_min,
      phone_first: seeded.phone_first,
      repeat: seeded.repeat,
    });
  }

  const d = draft;
  const submit = (body: Parameters<typeof save.mutate>[0]) => save.mutate(body);

  const nextSwitch = seeded?.seconds_to_switch;
  const laneLabel = (m: string) =>
    m === "phones" ? "Phone numbers" : m === "emails" ? "Emails" : "Both lanes together";

  return (
    <section className={`${cardClass} mt-6`}>
      <div className="flex items-center gap-2">
        <Timer className="h-4 w-4 text-indigo-300" />
        <h2 className="text-[16px] font-semibold text-white">AI lane schedule</h2>
      </div>
      <p className="mt-1 text-[12px] text-slate-500">
        Control which harvester lane runs — <span className="text-slate-300">phones</span> is free
        license-board fetching (zero AI spend), <span className="text-slate-300">emails</span> is the
        AI research lane. Saved settings are picked up on the next harvester pass — no restart.
      </p>

      {lane.isLoading && (
        <div className="mt-4 flex items-center gap-2 py-4 text-[13px] text-slate-500">
          <Spinner /> Loading lane schedule…
        </div>
      )}
      {lane.isError && (
        <p className="mt-3 rounded-lg bg-rose-500/10 px-3 py-2 text-[13px] text-rose-300">
          Lane schedule unavailable: {(lane.error as Error).message}
        </p>
      )}

      {seeded && d && (
        <>
          <div className="mt-4 rounded-lg border border-white/10 bg-white/[0.03] p-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-[13px] font-medium text-slate-200">Harvester control</p>
                <p className="text-[11.5px] text-slate-400">Worker {seeded.worker_online ? "online" : "offline"} · Current setting: {seeded.control_mode ?? "unknown"}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {(["off", "on", "schedule"] as const).map((mode) => <button key={mode} type="button"
                  disabled={control.isPending || seeded.control_mode === mode}
                  onClick={() => control.mutate(mode)}
                  className="rounded-md border border-white/10 px-3 py-1.5 text-[12px] text-slate-200 hover:bg-white/[0.08] disabled:opacity-50">
                  {mode === "off" ? "Stop" : mode === "on" ? "Run" : "Use schedule"}
                </button>)}
              </div>
            </div>
            {control.isError && <p role="alert" className="mt-2 text-[12px] text-rose-300">{(control.error as Error).message}</p>}
          </div>
          {/* Live status */}
          <div className="mt-4 grid grid-cols-1 gap-2 rounded-lg border border-white/5 bg-black/20 p-3 text-[12.5px] sm:grid-cols-2 lg:grid-cols-4">
            <span className="text-slate-500">
              Running now:{" "}
              <span className="text-slate-200">{laneLabel(seeded.effective_mode)}</span>
            </span>
            <span className="text-slate-500">
              Saved mode: <span className="text-slate-200">{laneLabel(seeded.mode)}</span>
            </span>
            <span className="text-slate-500">
              Next switch:{" "}
              <span className="text-slate-200">
                {typeof nextSwitch === "number"
                  ? `in ${Math.floor(nextSwitch / 60)}m ${Math.round(nextSwitch % 60)}s`
                  : "— (no cycle)"}
              </span>
            </span>
            <span className="text-slate-500">
              Harvester:{" "}
              <span className={seeded.harvester_enabled ? "text-emerald-300" : "text-rose-300"}>
                {seeded.harvester_enabled
                  ? `enabled · pass every ${Math.round(seeded.interval_s / 60)} min`
                  : "disabled in .env"}
              </span>
            </span>
          </div>

          {seeded.one_time_done && (
            <p className="mt-2 rounded-lg bg-amber-500/10 px-3 py-2 text-[12.5px] text-amber-200">
              The one-time cycle has finished — both lanes are running again. Save again to start a
              new cycle.
            </p>
          )}
          {!seeded.harvester_enabled && (
            <p className="mt-2 rounded-lg bg-rose-500/10 px-3 py-2 text-[12.5px] text-rose-200">
              HARVESTER_ENABLED is false — no schedule can take effect until the harvester is on.
            </p>
          )}

          {/* Mode */}
          <div className="mt-5">
            <p className="text-[12.5px] font-medium text-slate-300">Which lane should run?</p>
            <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
              <LaneModeCard
                active={d.mode === "both"}
                icon={<Share2 className="h-3.5 w-3.5" />}
                title="Both lanes"
                hint="Phones + emails every pass (default)"
                onClick={() => setDraft({ ...d, mode: "both" })}
              />
              <LaneModeCard
                active={d.mode === "phones"}
                icon={<Phone className="h-3.5 w-3.5" />}
                title="Phones only"
                hint="Emails blocked — zero AI spend"
                onClick={() => setDraft({ ...d, mode: "phones" })}
              />
              <LaneModeCard
                active={d.mode === "emails"}
                icon={<Mail className="h-3.5 w-3.5" />}
                title="Emails only"
                hint="AI research lane only"
                onClick={() => setDraft({ ...d, mode: "emails" })}
              />
              <LaneModeCard
                active={d.mode === "auto"}
                icon={<Timer className="h-3.5 w-3.5" />}
                title="Automatic cycle"
                hint="Alternate the lanes on a timer"
                onClick={() => setDraft({ ...d, mode: "auto" })}
              />
            </div>
          </div>

          {/* Auto-cycle detail */}
          {d.mode === "auto" && (
            <div className="mt-4 rounded-lg border border-white/5 bg-black/20 p-3">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <label className="text-[12.5px] text-slate-400">
                  Phone minutes
                  <input
                    type="number"
                    min={1}
                    max={720}
                    value={d.phone_min}
                    onChange={(e) =>
                      setDraft({ ...d, phone_min: Number(e.target.value) })
                    }
                    className={`${inputClass} mt-1`}
                  />
                </label>
                <label className="text-[12.5px] text-slate-400">
                  Email minutes
                  <input
                    type="number"
                    min={1}
                    max={720}
                    value={d.email_min}
                    onChange={(e) =>
                      setDraft({ ...d, email_min: Number(e.target.value) })
                    }
                    className={`${inputClass} mt-1`}
                  />
                </label>
                <label className="text-[12.5px] text-slate-400">
                  Which lane starts the cycle?
                  <Select
                    value={d.phone_first ? "phones" : "emails"}
                    onChange={(v) => setDraft({ ...d, phone_first: v === "phones" })}
                    options={[
                      { value: "phones", label: "Phone numbers first" },
                      { value: "emails", label: "Emails first" },
                    ]}
                    className="mt-1"
                  />
                </label>
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-1.5">
                <span className="text-[11.5px] text-slate-500">Presets:</span>
                {[
                  { p: 10, e: 40 },
                  { p: 30, e: 90 },
                  { p: 60, e: 60 },
                  { p: 120, e: 120 },
                ].map(({ p, e }) => (
                  <button
                    key={`${p}-${e}`}
                    onClick={() => setDraft({ ...d, phone_min: p, email_min: e })}
                    className="rounded-lg border border-white/10 px-2 py-1 text-[11.5px] text-slate-300 hover:bg-white/[0.04]"
                  >
                    {p}m phones / {e}m emails
                  </button>
                ))}
              </div>

              <div className="mt-4">
                <p className="text-[12.5px] font-medium text-slate-300">How long should it run?</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  <RepeatCard
                    active={d.repeat === "day"}
                    title="Full day (repeats)"
                    hint="The cycle keeps alternating all day"
                    onClick={() => setDraft({ ...d, repeat: "day" })}
                  />
                  <RepeatCard
                    active={d.repeat === "once"}
                    title="One time only"
                    hint="Runs a single cycle, then back to both lanes"
                    onClick={() => setDraft({ ...d, repeat: "once" })}
                  />
                </div>
              </div>

              <p className="mt-3 text-[11.5px] text-slate-500">
                {d.mode === "auto" && Number.isFinite(d.phone_min) && Number.isFinite(d.email_min)
                  ? `Cycle = ${d.phone_min + d.email_min} min — starts the moment you save.`
                  : ""}
              </p>
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
              onClick={() => submit(d)}
              disabled={save.isPending}
              className="rounded-lg bg-indigo-500/90 px-3.5 py-2 text-[13px] font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {save.isPending ? "Saving…" : "Save schedule"}
            </button>
            <button
              onClick={() => {
                // Send the full draft with mode=both: the slot minutes are
                // unused in a pinned mode, so this resets WHICH lane runs
                // without throwing away the timings the operator set up.
                const reset = { ...d, mode: "both" as AdminLaneMode };
                setDraft(reset);
                submit(reset);
              }}
              disabled={save.isPending}
              className="rounded-lg border border-white/10 px-3.5 py-2 text-[13px] text-slate-300 hover:bg-white/[0.04] disabled:opacity-50"
            >
              Reset to both lanes
            </button>
            {save.isSuccess && !save.isPending && (
              <span className="text-[12px] text-emerald-300">
                Saved — applies on the next pass.
              </span>
            )}
          </div>
          {save.isError && (
            <p className="mt-2 rounded-lg bg-rose-500/10 px-3 py-2 text-[12.5px] text-rose-300">
              Rejected: {(save.error as Error).message}
            </p>
          )}

          <ul className="mt-4 space-y-1 text-[11.5px] text-slate-500">
            {seeded.notes.map((n) => (
              <li key={n}>• {n}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function LaneModeCard({
  active,
  icon,
  title,
  hint,
  onClick,
}: {
  active: boolean;
  icon: ReactNode;
  title: string;
  hint: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-lg border p-3 text-left transition-colors ${
        active
          ? "border-indigo-400/60 bg-indigo-500/10"
          : "border-white/10 hover:bg-white/[0.03]"
      }`}
    >
      <span className="flex items-center gap-1.5 text-[13px] text-slate-200">
        {icon} {title}
      </span>
      <span className="mt-1 block text-[11.5px] text-slate-500">{hint}</span>
    </button>
  );
}

function RepeatCard({
  active,
  title,
  hint,
  onClick,
}: {
  active: boolean;
  title: string;
  hint: string;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-lg border px-3 py-2 text-left transition-colors ${
        active ? "border-indigo-400/60 bg-indigo-500/10" : "border-white/10 hover:bg-white/[0.03]"
      }`}
    >
      <span className="block text-[12.5px] text-slate-200">{title}</span>
      <span className="block text-[11.5px] text-slate-500">{hint}</span>
    </button>
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
