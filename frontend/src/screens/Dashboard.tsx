import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Building2, Users, Target, Gauge, TrendingUp, ArrowRight } from "lucide-react";
import { api } from "../api/client";
import { Spinner } from "../components/StatusChip";
import { STATE_LABEL, scoreColor, timeAgo, elapsed } from "../lib/format";

interface Stat {
  label: string;
  value: string;
  sub: string;
  icon: React.ElementType;
  to: string;
}

export default function Dashboard() {
  const navigate = useNavigate();

  const leadsQ = useQuery({
    queryKey: ["dash-leads"],
    // folder="*" = every place (inbox + all folders): foldering a lead is a
    // MOVE out of the default Companies inbox, but the Dashboard overview must
    // still count the whole researched universe.
    queryFn: () => api.listLeads({ folder: "*", limit: 1000 }),
  });
  const jobsQ = useQuery({
    queryKey: ["dash-jobs"],
    queryFn: () => api.listJobs(),
    refetchInterval: 60_000, // refresh every 60s so timestamps stay current
  });

  const leads = leadsQ.data ?? [];
  const jobs = jobsQ.data ?? [];

  const stats: Stat[] = useMemo(() => {
    const bound = leads.filter((l) => l.bound).length;
    const contactNow = leads.filter((l) => l.recommendation === "contact_now").length;
    const avg =
      leads.length > 0
        ? (leads.reduce((s, l) => s + l.score, 0) / leads.length).toFixed(1)
        : "0";
    return [
      {
        label: "Total Companies",
        value: leads.length.toLocaleString(),
        sub: "researched leads",
        icon: Building2,
        to: "/leads?folder=*",
      },
      {
        label: "Bound Contacts",
        value: bound.toLocaleString(),
        sub: "reachable decision-makers",
        icon: Users,
        to: "/leads?folder=*&bound=true",
      },
      {
        label: "Contact Now",
        value: contactNow.toLocaleString(),
        sub: "in a buying window",
        icon: Target,
        to: "/leads?folder=*&recommendation=contact_now",
      },
      {
        label: "Avg Score",
        value: avg,
        sub: "0–10 potential",
        icon: Gauge,
        to: "/leads?folder=*",
      },
    ];
  }, [leads]);

  const topLeads = useMemo(
    () =>
      leads
        .filter((l) => l.recommendation === "contact_now" && l.bound)
        .sort((a, b) => b.score - a.score)
        .slice(0, 5),
    [leads],
  );

  const recentJobs = useMemo(
    () => [...jobs].sort((a, b) => (b.created_at > a.created_at ? 1 : -1)).slice(0, 5),
    [jobs],
  );

  const loading = leadsQ.isLoading || jobsQ.isLoading;

  return (
    <div className="px-8 py-7 max-w-6xl">
      <h1 className="text-[26px] font-semibold text-white">Dashboard</h1>
      <p className="text-slate-500 text-[13.5px] mt-1">Welcome to LeadHunter Pro AI</p>

      {loading ? (
        <div className="flex items-center gap-2 text-slate-500 text-sm py-16 justify-center">
          <Spinner /> Loading live data…
        </div>
      ) : (
        <>
          {/* Stats */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mt-6">
            {stats.map(({ label, value, sub, icon: Icon, to }) => (
              <div
                key={label}
                onClick={() => navigate(to)}
                className="cursor-pointer rounded-xl border border-white/5 bg-white/[0.02] p-5 hover:bg-white/[0.04] hover:border-indigo-500/30 transition-colors"
              >
                <div className="flex items-start justify-between">
                  <span className="text-[13px] text-slate-400">{label}</span>
                  <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
                    <Icon className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
                  </div>
                </div>
                <div className="text-[28px] font-semibold text-white mt-3">{value}</div>
                <div className="flex items-center gap-1 mt-2 text-[12.5px] text-emerald-400">
                  <TrendingUp className="w-3.5 h-3.5" />
                  {sub}
                </div>
              </div>
            ))}
          </div>

          {/* Bottom row */}
          <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-5 mt-6">
            {/* Recent runs */}
            <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-[16px] font-semibold text-white">Recent Runs</h2>
                <button
                  onClick={() => navigate("/history")}
                  className="flex items-center gap-1 text-[12.5px] text-indigo-400 hover:text-indigo-300"
                >
                  View all <ArrowRight className="w-3.5 h-3.5" />
                </button>
              </div>
              {recentJobs.length === 0 ? (
                <p className="text-[13px] text-slate-500">No runs yet — start one from Research.</p>
              ) : (
                <div className="flex flex-col gap-2.5">
                  {recentJobs.map((j) => (
                    <div
                      key={j.id}
                      className="flex items-center justify-between rounded-lg bg-white/[0.02] border border-white/5 px-4 py-3.5"
                    >
                      <div className="min-w-0">
                        <div className="text-[14px] text-white font-medium truncate">
                          {j.query.trade} · {j.query.location}
                        </div>
                        <div className="text-[12.5px] text-slate-500 mt-0.5">
                          {timeAgo(j.created_at)} · {elapsed(j.elapsed_s)}
                        </div>
                      </div>
                      <StateBadge state={j.state} />
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Qualified pipeline */}
            <div className="rounded-xl border border-white/5 bg-white/[0.02] p-5">
              <h2 className="text-[16px] font-semibold text-white mb-4">
                Contact Now <span className="text-slate-500 font-normal">({topLeads.length})</span>
              </h2>
              {topLeads.length === 0 ? (
                <p className="text-[13px] text-slate-500">
                  No buying-window leads yet. Run a search in{" "}
                  <button onClick={() => navigate("/research")} className="text-indigo-400 hover:underline">
                    Research
                  </button>
                  .
                </p>
              ) : (
                <ul className="flex flex-col gap-2.5">
                  {topLeads.map((l) => (
                    <li
                      key={l.email}
                      onClick={() =>
                        // Back from the dossier returns to the dashboard.
                        navigate(`/leads/${encodeURIComponent(l.email)}`, {
                          state: { from: "/" },
                        })
                      }
                      className="cursor-pointer rounded-lg bg-white/[0.02] border border-white/5 px-3.5 py-3 hover:bg-white/[0.04]"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[13.5px] text-white font-medium truncate">
                          {l.company}
                        </span>
                        <span className={`text-[13px] font-semibold ${scoreColor(l.score)}`}>
                          {l.score.toFixed(1)}
                        </span>
                      </div>
                      <p className="text-[12px] text-slate-500 truncate mt-0.5">
                        {l.person} · {l.email}
                      </p>
                      {l.linkedin && (
                        <p className="text-[12px] text-indigo-400 truncate mt-1">{l.linkedin}</p>
                      )}
                      {l.reason && (
                        <p className="text-[12px] text-slate-400 line-clamp-2 mt-1">{l.reason}</p>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function StateBadge({ state }: { state: string }) {
  const styles: Record<string, string> = {
    completed: "text-emerald-400 bg-emerald-500/10",
    running: "text-sky-400 bg-sky-500/10 animate-pulse",
    failed: "text-rose-400 bg-rose-500/10",
    cancelled: "text-amber-400 bg-amber-500/10",
    queued: "text-slate-400 bg-slate-500/10",
  };
  return (
    <span className={`text-[12px] font-semibold px-2.5 py-1 rounded-md ${styles[state] ?? styles.queued}`}>
      {STATE_LABEL[state] ?? state}
    </span>
  );
}
