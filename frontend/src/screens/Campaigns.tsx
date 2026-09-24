import { useEffect, useRef, useState } from "react";
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
  Eye,
  Pencil,
  ShieldCheck,
  Wand2,
} from "lucide-react";
import { ApiError, api } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import type {
  Campaign,
  CampaignSend,
  CampaignUpdateInput,
  EmailAccount,
  LeadSummary,
  SpamFinding,
} from "../types";
import type { FollowupInput } from "../types";

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

const INPUT =
  "w-full bg-white/[0.04] border border-white/5 rounded-lg px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40";

/** One send row's status chip. */
function sendStatus(s: CampaignSend): { label: string; cls: string } {
  if (s.replied_at)
    return { label: "Replied", cls: "bg-amber-500/15 text-amber-300" };
  if (s.state === "sent")
    return { label: "Sent", cls: "bg-emerald-500/15 text-emerald-300" };
  if (s.state === "pending")
    return { label: "Queued", cls: "bg-sky-500/15 text-sky-300" };
  if (s.state === "failed")
    return { label: "Failed", cls: "bg-rose-500/15 text-rose-300" };
  return {
    label: s.error === "lead replied" ? "Stopped (replied)" : "Cancelled",
    cls: "bg-slate-500/15 text-slate-400",
  };
}

function stepLabel(step: number): string {
  return step === 0 ? "Email" : `Follow-up ${step}`;
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

  // Sending-account addresses for the per-send "Via" column.
  const accountsById = useQuery({
    queryKey: ["email-accounts"],
    queryFn: () => api.emailAccounts(),
    select: (list: EmailAccount[]) =>
      Object.fromEntries(list.map((a) => [a.id, a.email])) as Record<number, string>,
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
    <div className="workspace-page page-focused">
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
          Sends go out with a random 3–7 minute gap and a daily cap per Gmail
          account — add more sending accounts to scale volume safely. Every
          send lands on the lead's CRM timeline.
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
      {campaigns.isError && <div role="alert" className="mt-5 rounded-xl border border-rose-400/20 bg-rose-500/10 p-4 text-sm text-rose-200">Campaigns could not be loaded. <button className="underline ml-2" onClick={() => void campaigns.refetch()}>Try again</button></div>}

      {campaigns.data && campaigns.data.length === 0 && !building && (
        <div className="mt-6 ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-8 text-center">
          <Send className="w-8 h-8 text-slate-600 mx-auto" />
          <p className="mt-3 text-[14px] text-slate-400">No campaigns yet.</p>
          <p className="mt-1 text-[12.5px] text-slate-500">
            Connect a Gmail in Settings, then create your first outreach run.
          </p>
        </div>
      )}

      <div className="mt-5 space-y-3">
        {(campaigns.data || []).map((c) => (
          <CampaignCard
            key={c.id}
            c={c}
            accountsById={accountsById.data || {}}
            onPause={() => pause.mutate(c.id)}
            onResume={() => resume.mutate(c.id)}
            onRemove={() => remove.mutate(c.id)}
            actionsPending={pause.isPending || resume.isPending || remove.isPending}
          />
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// One campaign card — progress + the Sends log + the Edit-pitch form
// ---------------------------------------------------------------------------

function CampaignCard({
  c,
  accountsById,
  onPause,
  onResume,
  onRemove,
  actionsPending,
}: {
  c: Campaign;
  accountsById: Record<number, string>;
  onPause: () => void;
  onResume: () => void;
  onRemove: () => void;
  actionsPending: boolean;
}) {
  const [showSends, setShowSends] = useState(false);
  const [editing, setEditing] = useState(false);
  const queryClient = useQueryClient();

  const total = c.pending + c.sent + c.failed;
  const pct = total > 0 ? Math.round((c.sent / total) * 100) : 0;

  // The send log — every lead, its step, when it went out, opened, replied.
  const detail = useQuery({
    queryKey: ["campaign", c.id],
    queryFn: () => api.getCampaign(c.id),
    enabled: showSends,
    refetchInterval: 15000,
  });
  const sends: CampaignSend[] = detail.data?.sends || [];

  const edit = useMutation({
    mutationFn: (input: CampaignUpdateInput) => api.updateCampaign(c.id, input),
    onSuccess: () => {
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["campaigns"] });
      queryClient.invalidateQueries({ queryKey: ["campaign", c.id] });
    },
  });

  return (
    <div className="ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5">
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
            from {c.account_email || `account #${c.account_id}`}
            {(c.account_emails?.length || 0) > 1 &&
              ` +${c.account_emails.length - 1} more`}
            {c.ai_personalize && " · AI opening lines"}
            {" · starts "}
            {fmtLocal(c.start_at)}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => {
              setShowSends((v) => !v);
              setEditing(false);
            }}
            className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12.5px] hover:bg-white/[0.04] ${
              showSends
                ? "border-indigo-500/30 text-indigo-300"
                : "border-white/5 text-slate-300"
            }`}
          >
            <Eye className="w-3.5 h-3.5" />
            Sends
          </button>
          {c.status !== "completed" && (
            <button
              onClick={() => {
                setEditing((v) => !v);
                setShowSends(false);
              }}
              className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[12.5px] hover:bg-white/[0.04] ${
                editing
                  ? "border-indigo-500/30 text-indigo-300"
                  : "border-white/5 text-slate-300"
              }`}
            >
              <Pencil className="w-3.5 h-3.5" />
              Edit pitch
            </button>
          )}
          {(c.status === "running" || c.status === "scheduled") && (
            <button
              onClick={onPause}
              disabled={actionsPending}
              className="flex items-center gap-1.5 rounded-lg border border-white/5 px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.04]"
            >
              <Pause className="w-3.5 h-3.5" />
              Pause
            </button>
          )}
          {c.status === "paused" && (
            <button
              onClick={onResume}
              disabled={actionsPending}
              className="flex items-center gap-1.5 rounded-lg border border-white/5 px-3 py-1.5 text-[12.5px] text-emerald-300 hover:bg-emerald-500/10"
            >
              <Play className="w-3.5 h-3.5" />
              Resume
            </button>
          )}
          <button
            onClick={onRemove}
            disabled={actionsPending}
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
            {c.replied > 0 && ` · ${c.replied} replied`}
            {c.failed > 0 && ` · ${c.failed} failed`}
            {c.skipped > 0 && ` · ${c.skipped} skipped (replied)`}
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

      {editing && <EditPitchForm c={c} onSave={(input) => edit.mutate(input)} saving={edit.isPending} />}

      {showSends && (
        <div className="mt-4">
          {detail.isLoading ? (
            <p className="text-[12.5px] text-slate-500">Loading sends…</p>
          ) : detail.isError ? (
            <p className="text-[12.5px] text-rose-400">Could not load sends.</p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-white/5">
              <table className="w-full text-[12px]">
                <thead>
                  <tr className="text-left text-slate-500 border-b border-white/5">
                    <th className="px-3 py-2 font-medium">Lead</th>
                    <th className="px-3 py-2 font-medium">Step</th>
                    <th className="px-3 py-2 font-medium">Status</th>
                    <th className="px-3 py-2 font-medium">Sent</th>
                    <th className="px-3 py-2 font-medium">Follow-up due</th>
                    <th className="px-3 py-2 font-medium">Opened</th>
                    <th className="px-3 py-2 font-medium">Replied</th>
                    <th className="px-3 py-2 font-medium">Via</th>
                  </tr>
                </thead>
                <tbody>
                  {sends.map((s) => {
                    const st = sendStatus(s);
                    return (
                      <tr key={s.id} className="border-b border-white/5 last:border-0">
                        <td className="px-3 py-2 text-slate-300 whitespace-nowrap">{s.email}</td>
                        <td className="px-3 py-2 text-slate-400 whitespace-nowrap">{stepLabel(s.step)}</td>
                        <td className="px-3 py-2 whitespace-nowrap">
                          <span
                            className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${st.cls}`}
                          >
                            {st.label}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-slate-400 whitespace-nowrap">
                          {fmtLocal(s.sent_at) || "—"}
                        </td>
                        <td className="px-3 py-2 text-slate-400 whitespace-nowrap">
                          {s.state === "pending" && s.not_before
                            ? fmtLocal(s.not_before)
                            : "—"}
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap">
                          {s.opened_at ? (
                            <span className="text-emerald-300">
                              {fmtLocal(s.opened_at)}
                              {s.opened_count > 1 && ` (${s.opened_count}x)`}
                            </span>
                          ) : s.state === "sent" ? (
                            <span className="text-slate-500">Not yet</span>
                          ) : (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2 whitespace-nowrap">
                          {s.replied_at ? (
                            <span className="text-amber-300">{fmtLocal(s.replied_at)}</span>
                          ) : (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                        <td className="px-3 py-2 text-slate-400 whitespace-nowrap">
                          {s.account_id
                            ? accountsById[s.account_id] || `account #${s.account_id}`
                            : "—"}
                        </td>
                      </tr>
                    );
                  })}
                  {sends.length === 0 && (
                    <tr>
                      <td colSpan={8} className="px-3 py-3 text-slate-500">
                        No sends queued yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
          <p className="mt-2 text-[11.5px] text-slate-500">
            "Opened" comes from the tracking image in the email — a signal, not
            a proof (some inboxes block or pre-fetch images). "Not yet" means
            no open recorded so far.
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Edit the pitch of a started campaign (applies to not-yet-sent emails)
// ---------------------------------------------------------------------------

function EditPitchForm({
  c,
  onSave,
  saving,
}: {
  c: Campaign;
  onSave: (input: CampaignUpdateInput) => void;
  saving: boolean;
}) {
  const [name, setName] = useState(c.name);
  const [subject, setSubject] = useState(c.subject);
  const [body, setBody] = useState(c.body);

  const canSave =
    name.trim() !== "" && subject.trim() !== "" && body.trim() !== "" && !saving;

  return (
    <div className="mt-4 rounded-lg border border-indigo-500/20 bg-white/[0.02] p-3.5">
      <p className="text-[12px] font-semibold text-slate-300">Edit pitch</p>
      <p className="mt-1 text-[11.5px] text-slate-500">
        Applies to emails that have not gone out yet. Emails already sent keep
        their own record. The follow-up ladder stays as it is.
      </p>
      <div className="mt-2.5 space-y-2">
        <input
          className={INPUT}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Campaign name"
        />
        <input
          className={INPUT}
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          placeholder="Subject"
        />
        <textarea
          className={`${INPUT} h-32 resize-y`}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          placeholder="Email script"
        />
        <div className="mt-2.5">
          <SpamPanel
            subject={subject}
            body={body}
            onApply={(s, b) => {
              setSubject(s);
              setBody(b);
            }}
          />
        </div>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => onSave({ name: name.trim(), subject, body })}
            disabled={!canSave}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-[12.5px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save pitch"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Spam risk analyzer — live score, the mistakes behind it, one-click fix
// ---------------------------------------------------------------------------

const SPAM_LEVEL_STYLES: Record<string, { chip: string; bar: string; label: string }> = {
  low: {
    chip: "bg-emerald-500/15 text-emerald-300",
    bar: "bg-emerald-500",
    label: "Low risk",
  },
  medium: {
    chip: "bg-amber-500/15 text-amber-300",
    bar: "bg-amber-500",
    label: "Medium risk",
  },
  high: {
    chip: "bg-rose-500/15 text-rose-300",
    bar: "bg-rose-500",
    label: "High risk",
  },
};

const SPAM_SEVERITY_DOT: Record<string, string> = {
  high: "bg-rose-400",
  medium: "bg-amber-400",
  low: "bg-slate-400",
};

function SpamPanel({
  subject,
  body,
  onApply,
}: {
  subject: string;
  body: string;
  onApply: (subject: string, body: string) => void;
}) {
  // Debounced copy of the draft — the analyzer sees the script only after
  // the user stops typing, not on every keystroke.
  const [debounced, setDebounced] = useState({ subject, body });
  useEffect(() => {
    const t = setTimeout(() => setDebounced({ subject, body }), 600);
    return () => clearTimeout(t);
  }, [subject, body]);

  const [fixed, setFixed] = useState<string | null>(null);
  // What the last one-click fix applied — the banner survives until the
  // user edits AWAY from that text (a manual edit clears it).
  const appliedRef = useRef<{ subject: string; body: string } | null>(null);

  const check = useQuery({
    queryKey: ["spam-check", debounced.subject, debounced.body],
    queryFn: () => api.spamCheck(debounced),
    enabled: debounced.subject.trim() !== "" && debounced.body.trim() !== "",
  });

  const improve = useMutation({
    mutationFn: () => api.spamImprove({ subject, body }),
    onSuccess: (r) => {
      appliedRef.current = { subject: r.subject, body: r.body };
      onApply(r.subject, r.body);
      setFixed(
        r.method === "ai"
          ? "Rewritten by AI with the spam triggers removed — review it above."
          : `Fixed: ${r.notes.join("; ") || "spam triggers removed"}`,
      );
    },
    onError: () => setFixed(null),
  });

  // A manual edit after a fix clears the confirmation banner.
  useEffect(() => {
    const applied = appliedRef.current;
    if (applied && (subject !== applied.subject || body !== applied.body)) {
      appliedRef.current = null;
      setFixed(null);
    }
  }, [subject, body]);

  const r = check.data;
  const style = r ? SPAM_LEVEL_STYLES[r.level] || SPAM_LEVEL_STYLES.low : null;

  return (
    <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3.5">
      <div className="flex items-center justify-between">
        <p className="flex items-center gap-2 text-[12px] font-semibold text-slate-300">
          <ShieldCheck className="w-4 h-4 text-indigo-400" />
          Spam risk
        </p>
        {r && style && (
          <span
            className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${style.chip}`}
          >
            {style.label} · {r.score}%
          </span>
        )}
      </div>

      {check.isLoading && (
        <p className="mt-2 text-[11.5px] text-slate-500">
          Analyzing the script… (AI reads it like a deliverability expert,
          this can take a few seconds)
        </p>
      )}

      {check.isError && (
        <p className="mt-2 text-[11.5px] text-rose-400">
          Could not analyze — try again in a moment.
        </p>
      )}

      {!check.isLoading && !check.data &&
        (subject.trim() === "" || body.trim() === "") && (
          <p className="mt-2 text-[11.5px] text-slate-500">
            Type a subject and a script — the spam risk appears here as you
            write.
          </p>
        )}

      {r && style && (
        <>
          <div className="mt-2.5 h-1.5 rounded-full bg-white/5 overflow-hidden">
            <div
              className={`h-full rounded-full ${style.bar} transition-all`}
              style={{ width: `${r.score}%` }}
            />
          </div>

          {r.summary && (
            <p className="mt-2.5 text-[12px] italic text-slate-400">
              “{r.summary}”
            </p>
          )}

          {Object.keys(r.categories || {}).length > 0 && (
            <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-2">
              {Object.entries(r.categories).map(([name, val]) => {
                const v = Math.max(0, Math.min(100, val));
                const cls =
                  v >= 50
                    ? "bg-rose-500"
                    : v >= 25
                      ? "bg-amber-500"
                      : "bg-emerald-500";
                return (
                  <div key={name}>
                    <div className="flex items-center justify-between text-[11px] text-slate-500">
                      <span className="capitalize">{name}</span>
                      <span>{v}</span>
                    </div>
                    <div className="mt-1 h-1 rounded-full bg-white/5 overflow-hidden">
                      <div
                        className={`h-full rounded-full ${cls} transition-all`}
                        style={{ width: `${v}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {r.findings.length === 0 ? (
            <p className="mt-2.5 text-[12px] text-emerald-300">
              No spam triggers found — this script reads like a normal
              professional email.
            </p>
          ) : (
            <>
              <p className="mt-2.5 text-[11.5px] text-slate-500">
                What could trigger a spam filter:
              </p>
              <ul className="mt-1.5 space-y-1.5">
                {r.findings.map((f: SpamFinding) => (
                  <li key={f.rule} className="text-[12px] text-slate-300">
                    <div className="flex items-start gap-2">
                      <span
                        className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${SPAM_SEVERITY_DOT[f.severity] || SPAM_SEVERITY_DOT.low}`}
                      />
                      <span>
                        {f.message}
                        {f.count > 1 && (
                          <span className="text-slate-500"> ({f.count}x)</span>
                        )}
                        {f.category && (
                          <span className="ml-1.5 rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-slate-500 capitalize">
                            {f.category}
                          </span>
                        )}
                      </span>
                    </div>
                    {f.fix && (
                      <p className="ml-3.5 mt-0.5 pl-1 text-[11.5px] text-slate-500">
                        Fix: {f.fix}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
              <button
                onClick={() => improve.mutate()}
                disabled={improve.isPending}
                className="mt-3 flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-[12.5px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                <Wand2 className="w-3.5 h-3.5" />
                {improve.isPending ? "Fixing…" : "Fix in one click"}
              </button>
              <p className="mt-1.5 text-[11px] text-slate-500">
                The improved script replaces the fields above — review it
                before scheduling.
              </p>
            </>
          )}

          {fixed && (
            <p className="mt-2 rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-3 py-2 text-[11.5px] text-emerald-300">
              {fixed}
            </p>
          )}

          <p className="mt-2 text-[11px] text-slate-600">
            {r.method === "ai"
              ? "AI deliverability analysis + our content rules."
              : "Rules-only analysis (AI unavailable right now)."}{" "}
            Estimated by us, not Gmail's real filter — a guide, not a
            guarantee.
          </p>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One follow-up rung editor
// ---------------------------------------------------------------------------

function FollowupEditor({
  label, on, setOn, days, setDays, subject, setSubject, body, setBody, input,
}: {
  label: string;
  on: boolean;
  setOn: (v: boolean) => void;
  days: number;
  setDays: (v: number) => void;
  subject: string;
  setSubject: (v: string) => void;
  body: string;
  setBody: (v: string) => void;
  input: string;
}) {
  return (
    <div className={`rounded-lg border px-3 py-2.5 ${on ? "border-indigo-500/20 bg-white/[0.02]" : "border-white/5"}`}>
      <div className="flex items-center gap-2.5">
        <label className="flex items-center gap-2 text-[12.5px] text-slate-300 cursor-pointer">
          <input
            type="checkbox"
            checked={on}
            onChange={(e) => setOn(e.target.checked)}
            className="accent-indigo-500 w-3.5 h-3.5"
          />
          {label}
        </label>
        {on && (
          <span className="flex items-center gap-1.5 text-[12px] text-slate-400">
            after
            <input
              type="number"
              min={1}
              max={30}
              value={days}
              onChange={(e) => setDays(Number(e.target.value) || 3)}
              className="w-16 bg-white/[0.04] border border-white/5 rounded px-2 py-1 text-[12px] text-slate-300 outline-none focus:ring-2 focus:ring-indigo-500/40"
            />
            days
          </span>
        )}
      </div>
      {on && (
        <div className="mt-2.5 space-y-2">
          <input
            className={input}
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder="Subject — Re: {{company_name}} estimating"
          />
          <textarea
            className={`${input} h-20 resize-y`}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            placeholder={"Hi {{first_name}}, just bumping this to the top of your inbox…"}
          />
        </div>
      )}
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
  // Extra sending accounts beyond the primary (Phase E5): the scheduler
  // spreads sends across all of them, so daily volume scales with N.
  const [extraIds, setExtraIds] = useState<number[]>([]);
  const [aiPersonalize, setAiPersonalize] = useState(false);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [folder, setFolder] = useState("");
  const [recommendation, setRecommendation] = useState("contact_now");
  const [startAt, setStartAt] = useState(defaultStart());
  const [dailyLimit, setDailyLimit] = useState(30);
  // Follow-up ladder (Phase E4): two optional rungs, day 3 and day 7 by
  // default. Each is cancelled the moment the lead replies.
  const [fu1On, setFu1On] = useState(false);
  const [fu1Days, setFu1Days] = useState(3);
  const [fu1Subject, setFu1Subject] = useState("");
  const [fu1Body, setFu1Body] = useState("");
  const [fu2On, setFu2On] = useState(false);
  const [fu2Days, setFu2Days] = useState(7);
  const [fu2Subject, setFu2Subject] = useState("");
  const [fu2Body, setFu2Body] = useState("");
  // Spam check: send the current draft to your own inbox before scheduling.
  const [testEmail, setTestEmail] = useState("");
  const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);
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
    mutationFn: () => {
      const followups: FollowupInput[] = [];
      if (fu1On && fu1Subject.trim() && fu1Body.trim()) {
        followups.push({ after_days: fu1Days, subject: fu1Subject, body: fu1Body });
      }
      if (fu2On && fu2Subject.trim() && fu2Body.trim()) {
        followups.push({ after_days: fu2Days, subject: fu2Subject, body: fu2Body });
      }
      return api.createCampaign({
        name: name.trim(),
        account_id: accountId!,
        account_ids: extraIds.filter((id) => id !== accountId),
        subject,
        body,
        emails: poolLeads.map((l) => l.email),
        start_at: new Date(startAt).toISOString(),
        daily_limit: dailyLimit,
        followups,
        ai_personalize: aiPersonalize,
      });
    },
    onSuccess: (r) => {
      const skipped =
        r.excluded > 0 ? ` (${r.excluded} lead(s) skipped — already emailed)` : "";
      onDone(true, `Campaign "${r.campaign.name}" scheduled for ${fmtLocal(r.campaign.start_at)}${skipped}.`);
    },
    onError: (e) =>
      setError(e instanceof ApiError ? e.message : "Could not create campaign"),
  });

  const testSend = useMutation({
    mutationFn: () =>
      api.campaignTestSend({
        account_id: accountId!,
        to_email: testEmail.trim(),
        subject,
        body,
      }),
    onSuccess: (r) =>
      setTestResult({
        ok: true,
        text: `Sent to ${r.to} — check that inbox (and its spam folder).`,
      }),
    onError: (e) =>
      setTestResult({
        ok: false,
        text: e instanceof ApiError ? e.message : "Test send failed — is the backend reachable?",
      }),
  });

  const canCreate =
    name.trim() && accountId && subject.trim() && body.trim() && poolLeads.length > 0 && startAt;

  const emailOk = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(testEmail.trim());
  const canTest = !!accountId && subject.trim() !== "" && body.trim() !== "" && emailOk;

  const input = INPUT;

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
              <label className="text-[12px] text-slate-400">From (primary Gmail account)</label>
              <select
                className={`${input} mt-1`}
                value={accountId ?? ""}
                onChange={(e) => {
                  const id = Number(e.target.value) || null;
                  setAccountId(id);
                  // The primary can never also be an extra.
                  if (id !== null) setExtraIds((prev) => prev.filter((x) => x !== id));
                }}
              >
                <option value="">Select account…</option>
                {connected.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.email}
                  </option>
                ))}
              </select>
              {accountId && connected.length > 1 && (
                <div className="mt-2 space-y-1.5">
                  <p className="text-[11.5px] text-slate-500">
                    Also send from (optional, max 4) — sends spread across all
                    accounts, each keeps its own daily limit:
                  </p>
                  {connected
                    .filter((a) => a.id !== accountId)
                    .map((a) => (
                      <label
                        key={a.id}
                        className="flex items-center gap-2 text-[12.5px] text-slate-300 cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          className="accent-indigo-500 w-3.5 h-3.5"
                          checked={extraIds.includes(a.id)}
                          onChange={(e) =>
                            setExtraIds((prev) =>
                              e.target.checked
                                ? prev.length >= 4
                                  ? prev
                                  : [...prev, a.id]
                                : prev.filter((x) => x !== a.id),
                            )
                          }
                        />
                        {a.email}
                      </label>
                    ))}
                </div>
              )}
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
            <label className="mt-2.5 flex items-start gap-2 text-[12.5px] text-slate-300 cursor-pointer">
              <input
                type="checkbox"
                className="accent-indigo-500 w-3.5 h-3.5 mt-0.5"
                checked={aiPersonalize}
                onChange={(e) => setAiPersonalize(e.target.checked)}
              />
              <span>
                <span className="font-medium">AI opening line</span>{" "}
                <span className="text-slate-500">
                  — each first email opens "Hi [name]," (person or company),
                  then one or two AI-written sentences from the lead's
                  VERIFIED dossier facts (news, projects, events with
                  sources), then your script below. Nothing is invented, no
                  "Hi" needed in your script (it's removed automatically),
                  and no AI-style dashes — plain professional text only. No
                  verified facts? The plain script goes out.
                </span>
              </span>
            </label>
          </div>

          {/* Spam risk — live score + the mistakes + the one-click fix. */}
          <div className="mt-3">
            <SpamPanel
              subject={subject}
              body={body}
              onApply={(s, b) => {
                setSubject(s);
                setBody(b);
              }}
            />
          </div>

          {/* Spam check — send the draft to your own inbox before scheduling. */}
          <div className="mt-3 rounded-lg border border-white/5 bg-white/[0.02] p-3.5">
            <p className="text-[12px] font-semibold text-slate-300">
              Inbox spam check (optional)
            </p>
            <p className="mt-1 text-[11.5px] text-slate-500">
              Send this draft to your own email and see where it lands —
              variables fill with a sample lead so you see the real email.
              Nothing is created or counted.
            </p>
            <div className="mt-2.5 flex gap-2">
              <input
                type="email"
                className={input}
                value={testEmail}
                onChange={(e) => {
                  setTestEmail(e.target.value);
                  setTestResult(null);
                }}
                placeholder="you@example.com"
              />
              <button
                onClick={() => testSend.mutate()}
                disabled={!canTest || testSend.isPending}
                className="shrink-0 rounded-lg border border-white/10 px-4 text-[12.5px] font-semibold text-slate-200 hover:bg-white/[0.06] disabled:opacity-50"
              >
                {testSend.isPending ? "Sending…" : "Send test"}
              </button>
            </div>
            {testResult && (
              <p
                className={`mt-2 text-[12px] ${
                  testResult.ok ? "text-emerald-400" : "text-rose-400"
                }`}
              >
                {testResult.text}
              </p>
            )}
          </div>

          {/* Follow-up ladder (Phase E4) — stops automatically on a reply. */}
          <div className="mt-4 rounded-lg border border-white/5 bg-white/[0.02] p-3.5">
            <p className="text-[12px] font-semibold text-slate-300">
              Follow-ups (optional)
            </p>
            <p className="mt-1 text-[11.5px] text-slate-500">
              Each fires N days after the previous email. The ladder stops the
              moment a lead replies (their CRM stage moves to “replied”).
            </p>
            <div className="mt-3 space-y-3">
              <FollowupEditor
                label="Follow-up 1"
                on={fu1On} setOn={setFu1On}
                days={fu1Days} setDays={setFu1Days}
                subject={fu1Subject} setSubject={setFu1Subject}
                body={fu1Body} setBody={setFu1Body}
                input={input}
              />
              {fu1On && (
                <FollowupEditor
                  label="Follow-up 2"
                  on={fu2On} setOn={setFu2On}
                  days={fu2Days} setDays={setFu2Days}
                  subject={fu2Subject} setSubject={setFu2Subject}
                  body={fu2Body} setBody={setFu2Body}
                  input={input}
                />
              )}
            </div>
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
