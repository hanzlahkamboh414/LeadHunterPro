import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Activity, CheckCircle2, ListChecks, PlayCircle } from "lucide-react";
import { api } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import type { JobSummary } from "../types";
import StatusChip, { Spinner } from "../components/StatusChip";
import { elapsed, formatTime, recommendationBadge, recommendationLabel, timeAgo } from "../lib/format";

export default function History() {
  const navigate = useNavigate();
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.listJobs(),
    refetchInterval: 10_000,
  });

  // Card-driven view controls: stateFilter narrows the list, byDelivered
  // re-orders it (working leads desc). "" = off — the honest default view.
  const [stateFilter, setStateFilter] = useState<"" | "active" | "completed">("");
  const [byDelivered, setByDelivered] = useState(false);

  const jobs = data ?? [];
  const running = jobs.filter((j) => j.state === "running" || j.state === "queued").length;
  const completed = jobs.filter((j) => j.state === "completed").length;
  const delivered = jobs.reduce((s, j) => s + (j.working_leads ?? 0), 0);

  const visibleJobs = useMemo(() => {
    let list = jobs;
    if (stateFilter === "active") list = list.filter((j) => j.state === "running" || j.state === "queued");
    else if (stateFilter === "completed") list = list.filter((j) => j.state === "completed");
    if (byDelivered) list = [...list].sort((a, b) => (b.working_leads ?? 0) - (a.working_leads ?? 0));
    return list;
  }, [jobs, stateFilter, byDelivered]);

  // Working leads delivered per day — contiguous 14-day window anchored on
  // the newest run day, so gaps show as honest zeros (same rule as the
  // Dashboard trend chart). "YYYY-MM-DD" keys sort lexicographically.
  const perDay = (() => {
    if (jobs.length === 0) return [];
    const byDay = new Map<string, number>();
    for (const j of jobs) {
      const day = j.created_at.slice(0, 10);
      byDay.set(day, (byDay.get(day) ?? 0) + (j.working_leads ?? 0));
    }
    const days = [...byDay.keys()].sort().slice(-14);
    if (days.length === 0) return [];
    // Fill every day in the window so the axis is continuous.
    const cur = new Date(days[0]);
    const out: { day: string; label: string; working: number }[] = [];
    while (out.length < days.length + 30) {
      const iso = cur.toISOString().slice(0, 10);
      if (iso > days[days.length - 1]) break;
      out.push({
        day: iso,
        label: iso.slice(5), // MM-DD — the year is implied by "recent"
        working: byDay.get(iso) ?? 0,
      });
      cur.setDate(cur.getDate() + 1);
    }
    return out;
  })();

  return (
    <div className="workspace-page page-focused">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <PageHeader
          eyebrow="LeadHunter Pro"
          title="Run History"
          subtitle="Past discovery + research runs."
        />
        <button
          onClick={() => refetch()}
          className="rounded-lg border border-white/5 px-3 py-2 text-[13px] text-slate-300 hover:bg-white/[0.04] inline-flex items-center gap-1"
        >
          {isFetching ? <Spinner className="h-4 w-4" /> : "↻"} Refresh
        </button>
      </div>

      {/* Stat cards + delivery trend — the run history at a glance,
          Dashboard-style. Every card ACTS: click filters/sorts the list. */}
      {jobs.length > 0 && (
        <>
          <div className="mt-5 grid grid-cols-2 lg:grid-cols-4 gap-3">
            <HistStat
              label={stateFilter || byDelivered ? "All runs (reset)" : "Total runs"}
              value={jobs.length.toLocaleString()}
              icon={Activity}
              active={!!stateFilter || byDelivered}
              onClick={() => {
                setStateFilter("");
                setByDelivered(false);
              }}
            />
            <HistStat
              label="Active now"
              value={running.toLocaleString()}
              icon={PlayCircle}
              tint="indigo"
              active={stateFilter === "active"}
              onClick={() => setStateFilter(stateFilter === "active" ? "" : "active")}
            />
            <HistStat
              label="Completed"
              value={completed.toLocaleString()}
              icon={CheckCircle2}
              tint="emerald"
              active={stateFilter === "completed"}
              onClick={() => setStateFilter(stateFilter === "completed" ? "" : "completed")}
            />
            <HistStat
              label={byDelivered ? "Delivered (sorted ↓)" : "Working leads delivered"}
              value={delivered.toLocaleString()}
              icon={ListChecks}
              tint="amber"
              active={byDelivered}
              onClick={() => setByDelivered((v) => !v)}
            />
          </div>

          <section className="mt-4 ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5">
            <h2 className="text-[16px] font-semibold text-white mb-1">Delivery — last 14 days</h2>
            <p className="text-[12px] text-slate-500 mb-3">
              Working leads delivered per day (only completed/paused runs contribute their totals).
            </p>
            <div className="h-44">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={perDay} margin={{ top: 4, right: 4, bottom: 0, left: -28 }}>
                  <CartesianGrid stroke="#1e2530" vertical={false} />
                  <XAxis
                    dataKey="label"
                    tick={{ fill: "#64748b", fontSize: 11 }}
                    tickLine={false}
                    axisLine={{ stroke: "#1e2530" }}
                    interval="preserveStartEnd"
                  />
                  <YAxis
                    allowDecimals={false}
                    tick={{ fill: "#64748b", fontSize: 11 }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <Tooltip
                    cursor={{ fill: "rgba(99,102,241,0.08)" }}
                    contentStyle={{
                      background: "#0d1117",
                      border: "1px solid rgba(255,255,255,0.1)",
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    labelStyle={{ color: "#94a3b8" }}
                  />
                  <Bar dataKey="working" name="working leads" fill="#818cf8" radius={[3, 3, 0, 0]} maxBarSize={28} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </section>
        </>
      )}

      {isError && (
        <p className="mt-4 text-[13px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2">
          Failed to load history: {(error as Error).message}
        </p>
      )}

      {isLoading ? (
        <div className="flex items-center gap-2 text-slate-500 text-sm py-16 justify-center">
          <Spinner /> Loading history…
        </div>
      ) : jobs.length === 0 ? (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center text-slate-500">
          No runs yet. Start one on the{" "}
          <button onClick={() => navigate("/research")} className="text-indigo-400 hover:underline">
            Research
          </button>{" "}
          screen.
        </div>
      ) : visibleJobs.length === 0 ? (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center text-slate-500 mt-4">
          No runs match this filter —{" "}
          <button
            onClick={() => setStateFilter("")}
            className="text-indigo-400 hover:underline"
          >
            show all
          </button>
          .
        </div>
      ) : (
        <>
          {(stateFilter || byDelivered) && (
            <p className="mt-4 text-[12.5px] text-indigo-300/90">
              {stateFilter === "active"
                ? "Showing active runs only"
                : stateFilter === "completed"
                  ? "Showing completed runs only"
                  : "Sorted by working leads delivered"}
              {" — "}
              <button
                onClick={() => {
                  setStateFilter("");
                  setByDelivered(false);
                }}
                className="underline hover:text-indigo-200"
              >
                reset
              </button>
            </p>
          )}
          <ul className="space-y-3">
            {visibleJobs.map((job) => (
              <HistoryRow key={job.id} job={job} />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

/** Compact stat card for the History overview strip — clickable cards act
 *  on the list (filter / sort / reset), with an active state while on. */
function HistStat({
  label,
  value,
  icon: Icon,
  tint = "slate",
  active = false,
  onClick,
}: {
  label: string;
  value: string;
  icon: typeof Activity;
  tint?: "emerald" | "amber" | "indigo" | "slate";
  active?: boolean;
  onClick?: () => void;
}) {
  const tintCls =
    tint === "emerald"
      ? "bg-emerald-500/15 text-emerald-300"
      : tint === "amber"
        ? "bg-amber-500/15 text-amber-300"
        : tint === "indigo"
          ? "bg-indigo-500/15 text-indigo-400"
          : "bg-slate-500/15 text-slate-400";
  const cls = `rounded-xl border p-4 text-left transition-colors ${
    onClick ? "cursor-pointer hover:bg-white/[0.04]" : ""
  } ${active ? "border-indigo-500/50 bg-indigo-500/[0.08]" : "border-white/5 bg-white/[0.02]"}`;
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12.5px] text-slate-400">{label}</span>
        <div className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${tintCls}`}>
          <Icon className="h-[15px] w-[15px]" strokeWidth={1.9} />
        </div>
      </div>
      <div className="mt-1.5 text-[22px] font-semibold text-white">{value}</div>
    </>
  );
  return onClick ? (
    <button type="button" onClick={onClick} className={cls}>
      {body}
    </button>
  ) : (
    <div className={cls}>{body}</div>
  );
}

function HistoryRow({ job }: { job: JobSummary }) {  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Full detail (results + events + pass_log) is fetched only on expand.
  const detail = useQuery({
    queryKey: ["job-detail", job.id],
    queryFn: () => api.getJob(job.id),
    enabled: open,
  });
  const d = detail.data;

  // Continue: resume a PAUSED run, or re-launch a job interrupted by a server
  // restart (marked failed). Same job id — prior results/progress are kept.
  const continueMut = useMutation({
    mutationFn: () => api.resumeJob(job.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["job-detail", job.id] });
      setOpen(true);
    },
  });

  // Pause / Cancel a RUNNING run straight from History (the same controls the
  // live Research view has — History is the recall view, not read-only).
  const pauseMut = useMutation({
    mutationFn: () => api.pauseJob(job.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["job-detail", job.id] });
    },
  });
  const cancelMut = useMutation({
    mutationFn: () => api.cancelJob(job.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["job-detail", job.id] });
    },
  });

  return (
    <li className="ui-panel rounded-xl border border-white/5 bg-white/[0.02]">
      <div className="flex items-stretch">
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex-1 min-w-0 flex items-center justify-between gap-3 px-4 py-3.5 text-left hover:bg-white/[0.03] rounded-xl"
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-[14px] font-medium text-white truncate">
                {job.query.trade} · {job.query.location}
              </span>
              <StatusChip state={job.state} />
            </div>
            <p className="text-[12.5px] text-slate-500 mt-0.5">
              {timeAgo(job.created_at)} ·{" "}
              {job.state === "completed" && (job.shortfall ?? 0) > 0 ? (
                <span className="text-amber-300/90">
                  {job.working_leads} of {job.query.target_emails} working —{" "}
                  {job.shortfall_reason || "under-delivered"}
                </span>
              ) : (
                <>target {job.query.target_emails} · {job.elapsed_s ? elapsed(job.elapsed_s) : "—"}</>
              )}
            </p>
          </div>
          <span className={`text-slate-500 transition-transform shrink-0 ${open ? "rotate-90" : ""}`}>›</span>
        </button>
        {(job.state === "paused" || job.state === "failed") && (
          <button
            onClick={() => continueMut.mutate()}
            disabled={continueMut.isPending}
            className="shrink-0 self-center mr-3 rounded-lg bg-indigo-500/90 hover:bg-indigo-500 text-white px-3 py-2 text-[12.5px] font-medium disabled:opacity-50 inline-flex items-center gap-1.5"
          >
            {continueMut.isPending ? <Spinner className="h-3.5 w-3.5" /> : "⏵"}
            {job.state === "paused" ? "Resume" : "Continue"}
          </button>
        )}
        {job.state === "running" && (
          <div className="shrink-0 self-center mr-3 flex items-center gap-2">
            <button
              onClick={() => pauseMut.mutate()}
              disabled={pauseMut.isPending}
              className="rounded-lg border border-amber-500/40 px-3 py-2 text-[12.5px] text-amber-300 hover:bg-amber-500/10 disabled:opacity-50"
            >
              {pauseMut.isPending ? "Pausing…" : "⏸ Pause"}
            </button>
            <button
              onClick={() => cancelMut.mutate()}
              disabled={cancelMut.isPending}
              className="rounded-lg border border-rose-500/40 px-3 py-2 text-[12.5px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
            >
              {cancelMut.isPending ? "Cancelling…" : "Cancel"}
            </button>
          </div>
        )}
        {job.state === "paused" && (
          <button
            onClick={() => cancelMut.mutate()}
            disabled={cancelMut.isPending}
            className="shrink-0 self-center mr-3 rounded-lg border border-rose-500/40 px-3 py-2 text-[12.5px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
          >
            {cancelMut.isPending ? "Cancelling…" : "Cancel"}
          </button>
        )}
      </div>

      {open && (
        <div className="border-t border-white/5 px-4 py-4">
          {detail.isLoading && (
            <div className="flex items-center gap-2 text-slate-500 text-sm">
              <Spinner className="h-4 w-4" /> Loading job…
            </div>
          )}
          {detail.isError && (
            <p className="text-[13px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2">
              {(detail.error as Error).message}
            </p>
          )}

          {d && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <div>
                <h3 className="text-[12px] font-semibold uppercase tracking-wide text-slate-500 mb-2">
                  Results ({d.results.length})
                </h3>
                {d.results.length === 0 ? (
                  <p className="text-[13px] text-slate-500">No leads.</p>
                ) : (
                  <ul className="space-y-2">
                    {d.results.map((r) => (
                      <li
                        key={r.email}
                        onClick={() =>
                          // Back from the dossier returns HERE, not to the leads
                          // list the detail screen defaults to.
                          navigate(`/leads/${encodeURIComponent(r.email)}`, {
                            state: { from: "/history" },
                          })
                        }
                        className="cursor-pointer rounded-lg border border-white/5 bg-[#0B0E14]/50 px-3 py-2 hover:bg-white/[0.04]"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-[13.5px] font-medium text-white truncate">{r.company}</span>
                          <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] ${recommendationBadge(r.recommendation).cls}`}>
                            <span className={`h-1.5 w-1.5 rounded-full ${recommendationBadge(r.recommendation).dot}`} />
                            {recommendationLabel(r.recommendation)}
                          </span>
                        </div>
                        <p className="text-[12px] text-slate-500 truncate mt-0.5">{r.email}</p>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div>
                <h3 className="text-[12px] font-semibold uppercase tracking-wide text-slate-500 mb-2">
                  Progress log ({d.events.length})
                </h3>
                {d.events.length === 0 ? (
                  <p className="text-[13px] text-slate-500">No events.</p>
                ) : (
                  <ul className="space-y-1 max-h-80 overflow-y-auto pr-1 font-mono text-[12px]">
                    {d.events.map((ev, i) => (
                      <li key={i} className="flex gap-2 text-slate-500">
                        <span className="text-slate-600 shrink-0">{formatTime(ev.ts)}</span>
                        <span className={ev.email ? "text-sky-300" : "text-slate-200"}>
                          {ev.phase}: {ev.message}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}
