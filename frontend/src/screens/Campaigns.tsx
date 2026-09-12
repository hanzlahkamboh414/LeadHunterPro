import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  Send,
  Pause,
  Play,
  Trash2,
  Plus,
  X,
  CheckCircle2,
  AlertTriangle,
  Info,
} from "lucide-react";
import { ApiError, api } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import type { EmailAccount, LeadSummary } from "../types";

/** Template variables — the ONLY facts that can appear in an email (all from
 * the lead's researched dossier; unknown/empty render blank, never
 * invented). */
const TEMPLATE_VARS = [
  "{{first_name}}",
  "{{last_name}}",
  "{{person_name}}",
  "{{role}}",
  "{{company_name}}",
  "{{location}}",
  "{{domain}}",
];

const STATUS_STYLES: Record<string, string> = {
  scheduled: "bg-sky-500/15 text-sky-300",
  running: "bg-emerald-500/15 text-emerald-300",
  paused: "bg-amber-500/15 text-amber-300",
  completed: "bg-slate-500/15 text-slate-300",
};

const PAUSE_REASONS: Record<string, string> = {
  user: "Paused by you",
  rate_limited: "Rate limited by Gmail — auto-resumes after cooldown",
  account: "Gmail account disconnected — resumes when reconnected",
};

function fmtLocal(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** datetime-local default: tomorrow at 09:00, the user's own clock. */
function defaultStart(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function Campaigns() {
  const [building, setBuilding] = useState(false);
  const [banner, setBanner] = useState<{ ok: boolean; text: string } | null>(null);
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const openNew = params.get("new") === "1";
  useEffect(() => {
    if (openNew) setBuilding(true);
  }, [openNew]);

  // Live-ish list: progress advances while the scheduler drains.
  const campaigns = useQuery({
    queryKey: ["campaigns"],
    queryFn: () => api.campaigns(),
    refetchInterval: 15000,
  });

  const pause = useMutation({
    mutationFn: (id: number) => api.pauseCampaign(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["campaigns"] }),
  });
  const resume = useMutation({
    mutationFn: (id: number) => api.resumeCampaign(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["campaigns"] }),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteCampaign(id),
    onSuccess: () => {
      setBanner({ ok: true, text: "Campaign deleted." });
      queryClient.invalidateQueries({ queryKey: ["campaigns"] });
    },
  });

  return (
    <div className="px-8 py-7 max-w-4xl">
      <PageHeader
        eyebrow="LeadHunter Pro"
        title="Campaigns"
        subtitle="Email outreach on your connected Gmail — paced slowly, capped daily, paused safely."
      />

      {banner && (
        <div
          className={`mt-4 flex items-start gap-2.5 rounded-lg border px-4 py-3 text-[13px] ${
            banner.ok
              ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
              : "border-rose-500/25 bg-rose-500/10 text-rose-300"
          }`}
        >
          {banner.ok ? (
            <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
          ) : (
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          )}
          <p className="flex-1">{banner.text}</p>
          <button onClick={() => setBanner(null)} className="text-slate-400 hover:text-slate-200">
            ✕
          </button>
        </div>
      )}

      <div className="mt-5 flex items-center justify-between">
        <p className="text-[13px] text-slate-500">
          Sends go out with a random 3–7 minute gap, max 30/day per Gmail
          account, and every send lands on the lead's CRM timeline.
        </p>
        <button
          onClick={() => setBuilding(true)}
          className="ml-4 shrink-0 flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500"
        >
          <Plus className="w-4 h-4" />
          New campaign
        </button>
      </div>

      {building && (
        <CampaignBuilder
          onDone={(ok, text) => {
            setBuilding(false);
            setBanner({ ok, text });
            queryClient.invalidateQueries({ queryKey: ["campaigns"] });
          }}
          onCancel={() => setBuilding(false)}
        />
      )}

      {campaigns.isLoading && (
        <p className="mt-6 text-[13px] text-slate-500">Loading campaigns…</p>
      )}

      {campaigns.data && campaigns.data.length === 0 && !building && (
        <div className="mt-6 rounded-xl border border-white/5 bg-white/[0.02] p-8 text-center">
          <Send className="w-8 h-8 text-slate-600 mx-auto" />
          <p className="mt-3 text-[14px] text-slate-400">No campaigns yet.</p>
          <p className="mt-1 text-[12.5px] text-slate-500">
            Connect a Gmail in Settings, then create your first outreach run.
          </p>
        </div>
      )}

      <div className="mt-5 space-y-3">
        {(campaigns.data || []).map((c) => {
          const total = c.pending + c.sent + c.failed;
          const pct = total > 0 ? Math.round((c.sent / total) * 100) : 0;
          return (
            <div key={c.id} className="rounded-xl border border-white/5 bg-white/[0.02] p-5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2.5">
                    <h2 className="text-[15px] font-semibold text-white truncate">{c.name}</h2>
                    <span
                      className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold ${STATUS_STYLES[c.status] || STATUS_STYLES.completed}`}
                    >
                      {c.status === "paused" && c.paused_reason
                        ? PAUSE_REASONS[c.paused_reason] || "Paused"
                        : c.status}
                    </span>
                  </div>
                  <p className="mt-1 text-[12.5px] text-slate-500 truncate">
                    from {c.account_email || `account #${c.account_id}`} · starts{" "}
                    {fmtLocal(c.start_at)}
                  </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {(c.status === "running" || c.status === "scheduled") && (
                    <button
                      onClick={() => pause.mutate(c.id)}
                      disabled={pause.isPending}
                      className="flex items-center gap-1.5 rounded-lg border border-white/5 px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.04]"
                    >
                      <Pause className="w-3.5 h-3.5" />
                      Pause
                    </button>
                  )}
                  {c.status === "paused" && (
                    <button
                      onClick={() => resume.mutate(c.id)}
                      disabled={resume.isPending}
                      className="flex items-center gap-1.5 rounded-lg border border-white/5 px-3 py-1.5 text-[12.5px] text-emerald-300 hover:bg-emerald-500/10"
                    >
                      <Play className="w-3.5 h-3.5" />
                      Resume
                    </button>
                  )}
                  <button
                    onClick={() => remove.mutate(c.id)}
                    disabled={remove.isPending}
                    className="flex items-center gap-1.5 rounded-lg border border-white/5 px-3 py-1.5 text-[12.5px] text-rose-400 hover:bg-rose-500/10"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>

              <div className="mt-4">
                <div className="flex items-center justify-between text-[12px] text-slate-500">
                  <span>
                    {c.sent} sent · {c.pending} pending
                    {c.failed > 0 && ` · ${c.failed} failed`}
                  </span>
                  <span>max {c.daily_limit}/day</span>
                </div>
                <div className="mt-1.5 h-1.5 rounded-full bg-white/5 overflow-hidden">
                  <div
                    className="h-full rounded-full bg-emerald-500 transition-all"
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The builder
// ---------------------------------------------------------------------------

function CampaignBuilder({
  onDone,
  onCancel,
}: {
  onDone: (ok: boolean, text: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [folder, setFolder] = useState("");
  const [recommendation, setRecommendation] = useState("contact_now");
  const [startAt, setStartAt] = useState(defaultStart());
  const [dailyLimit, setDailyLimit] = useState(30);
  const [error, setError] = useState("");

  const accounts = useQuery({ queryKey: ["email-accounts"], queryFn: () => api.emailAccounts() });
  const folders = useQuery({ queryKey: ["folders"], queryFn: () => api.listFolders() });

  const connected: EmailAccount[] = (accounts.data || []).filter(
    (a) => a.status === "connected",
  );

  // The lead pool for the current folder/recommendation pick (capped at 500 —
  // a campaign is a focused run, not a blast).
  const pool = useQuery({
    queryKey: ["campaign-pool", folder, recommendation],
    queryFn: () =>
      api.listLeads({
        folder: folder || undefined,
        recommendation: recommendation || undefined,
        limit: 500,
      }),
    enabled: connected.length > 0,
  });
  const poolLeads: LeadSummary[] = pool.data || [];

  const create = useMutation({
    mutationFn: () =>
      api.createCampaign({
        name: name.trim(),
        account_id: accountId!,
        subject,
        body,
        emails: poolLeads.map((l) => l.email),
        start_at: new Date(startAt).toISOString(),
        daily_limit: dailyLimit,
      }),
    onSuccess: (r) => {
      const skipped =
        r.excluded > 0 ? ` (${r.excluded} lead(s) skipped — already emailed)` : "";
      onDone(true, `Campaign "${r.campaign.name}" scheduled for ${fmtLocal(r.campaign.start_at)}${skipped}.`);
    },
    onError: (e) =>
      setError(e instanceof ApiError ? e.message : "Could not create campaign"),
  });

  const canCreate =
    name.trim() && accountId && subject.trim() && body.trim() && poolLeads.length > 0 && startAt;

  const input =
    "w-full bg-white/[0.04] border border-white/5 rounded-lg px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40";

  return (
    <div className="mt-5 rounded-xl border border-indigo-500/20 bg-white/[0.02] p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-[15px] font-semibold text-white">New campaign</h2>
        <button onClick={onCancel} className="text-slate-400 hover:text-slate-200">
          <X className="w-4 h-4" />
        </button>
      </div>

      {connected.length === 0 ? (
        <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-[12.5px] text-amber-300">
          <Info className="w-4 h-4 shrink-0 mt-0.5" />
          <p>No connected Gmail. Connect one in Settings first — campaigns send from it.</p>
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="text-[12px] text-slate-400">Campaign name</label>
              <input
                className={`${input} mt-1`}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Q3 GC outreach"
              />
            </div>
            <div>
              <label className="text-[12px] text-slate-400">From (Gmail account)</label>
              <select
                className={`${input} mt-1`}
                value={accountId ?? ""}
                onChange={(e) => setAccountId(Number(e.target.value) || null)}
              >
                <option value="">Select account…</option>
                {connected.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.email}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="mt-3">
            <label className="text-[12px] text-slate-400">Subject</label>
            <input
              className={`${input} mt-1`}
              value={subject}
              onChange={(e) => setSubject(e.target.value)}
              placeholder="Estimating for {{company_name}}"
            />
          </div>

          <div className="mt-3">
            <label className="text-[12px] text-slate-400">Email script</label>
            <textarea
              className={`${input} mt-1 h-32 resize-y`}
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder={"Hi {{first_name}},\n\nSaw {{company_name}} in {{location}}…"}
            />
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {TEMPLATE_VARS.map((v) => (
                <code
                  key={v}
                  className="rounded bg-white/[0.05] px-1.5 py-0.5 text-[11px] text-slate-400"
                >
                  {v}
                </code>
              ))}
            </div>
            <p className="mt-1.5 text-[11.5px] text-slate-500">
              Variables fill from the lead's researched dossier. Missing facts
              render blank — nothing is ever invented.
            </p>
          </div>

          <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="text-[12px] text-slate-400">Leads: folder</label>
              <select
                className={`${input} mt-1`}
                value={folder}
                onChange={(e) => setFolder(e.target.value)}
              >
                <option value="">All leads (unfiled + folders)</option>
                {(folders.data?.folders || []).map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.name} ({f.count})
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-[12px] text-slate-400">Recommendation</label>
              <select
                className={`${input} mt-1`}
                value={recommendation}
                onChange={(e) => setRecommendation(e.target.value)}
              >
                <option value="">Any</option>
                <option value="contact_now">Contact now</option>
                <option value="nurture">Nurture</option>
              </select>
            </div>
          </div>

          <div className="mt-2 rounded-lg bg-white/[0.03] border border-white/5 px-3.5 py-2.5 text-[12.5px] text-slate-400">
            {pool.isLoading ? (
              "Counting leads…"
            ) : (
              <>
                <span className="text-slate-200 font-semibold">{poolLeads.length}</span>{" "}
                lead(s) will be queued (first 500 max)
                {poolLeads.length > 0 && (
                  <span className="text-slate-500">
                    {" "}
                    — e.g. {poolLeads.slice(0, 3).map((l) => l.email).join(", ")}
                    {poolLeads.length > 3 && "…"}
                  </span>
                )}
              </>
            )}
          </div>

          <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div>
              <label className="text-[12px] text-slate-400">Start (your local time)</label>
              <input
                type="datetime-local"
                className={`${input} mt-1`}
                value={startAt}
                onChange={(e) => setStartAt(e.target.value)}
              />
            </div>
            <div>
              <label className="text-[12px] text-slate-400">Daily limit (per Gmail account)</label>
              <input
                type="number"
                min={1}
                max={200}
                className={`${input} mt-1`}
                value={dailyLimit}
                onChange={(e) => setDailyLimit(Number(e.target.value) || 30)}
              />
              <p className="mt-1.5 text-[11.5px] text-slate-500">
                Random 3–7 min gap between emails (fixed, account-safe).
              </p>
            </div>
          </div>

          {error && (
            <p className="mt-3 rounded-lg border border-rose-500/25 bg-rose-500/10 px-3.5 py-2.5 text-[12.5px] text-rose-300">
              {error}
            </p>
          )}

          <div className="mt-4 flex justify-end gap-2">
            <button
              onClick={onCancel}
              className="rounded-lg border border-white/5 px-4 py-2 text-[13px] text-slate-400 hover:bg-white/[0.04]"
            >
              Cancel
            </button>
            <button
              onClick={() => create.mutate()}
              disabled={!canCreate || create.isPending}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {create.isPending ? "Scheduling…" : "Schedule campaign"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
