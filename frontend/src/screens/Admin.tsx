import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Database, Gauge, RefreshCw, ShieldCheck } from "lucide-react";
import { api } from "../api/client";
import { Spinner } from "../components/StatusChip";

const cardClass =
  "rounded-xl border border-white/5 bg-white/[0.02] p-5";

function count(values: Record<string, number>, key: string): string {
  return (values[key] ?? 0).toLocaleString();
}

export default function Admin() {
  const dashboard = useQuery({
    queryKey: ["admin-dashboard"],
    queryFn: () => api.adminDashboard(),
    refetchInterval: 10_000,
  });
  const data = dashboard.data;

  return (
    <div className="px-8 py-7 max-w-6xl">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5 text-indigo-400" />
            <h1 className="text-[26px] font-semibold text-white">Admin Operations</h1>
          </div>
          <p className="text-slate-500 text-[13.5px] mt-1">
            Read-only health view of the existing lead pipeline and database.
          </p>
        </div>
        <button
          onClick={() => dashboard.refetch()}
          className="rounded-lg border border-white/5 px-3 py-2 text-[13px] text-slate-300 hover:bg-white/[0.04] inline-flex items-center gap-2"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

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
    </div>
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
