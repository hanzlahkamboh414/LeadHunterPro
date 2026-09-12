import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CornerDownRight, ExternalLink, Flag, StickyNote } from "lucide-react";
import { api } from "../api/client";
import { Select } from "../components/Select";
import type { CrmEvent, CrmInput, EvidenceFact } from "../types";
import { CRM_STAGES } from "../types";
import { Spinner } from "../components/StatusChip";
import {
  crmStageBadge,
  crmStageLabel,
  recommendationBadge,
  recommendationLabel,
  scoreColor,
  timeAgo,
} from "../lib/format";

export default function LeadDetail() {
  const { email = "" } = useParams();
  const decoded = decodeURIComponent(email);
  // Where the list was when this lead was opened — folder/tag/date filters and
  // all. A hardcoded "/leads" used to drop them, dumping the user back into the
  // Unfiled inbox instead of the folder they were browsing.
  const location = useLocation();
  const backTo = (location.state as { from?: string } | null)?.from || "/leads";

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["lead", decoded],
    queryFn: () => api.getLead(decoded),
  });

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-500 text-sm py-16 justify-center">
        <Spinner /> Loading dossier…
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="px-8 py-10 max-w-4xl">
        <p className="text-[13px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2">
          Could not load {decoded}: {(error as Error).message}
        </p>
        <Link to={backTo} className="inline-flex items-center gap-1.5 text-[13px] text-indigo-400 hover:underline mt-4">
          <ArrowLeft className="w-4 h-4" /> Back to leads
        </Link>
      </div>
    );
  }

  const badge = recommendationBadge(data.recommendation);
  // The CRM stage chip rides beside the AI recommendation — the AI's verdict
  // (what LeadHunter thinks) and the pipeline stage (where YOU are with this
  // lead) are two different answers, both visible at once.
  const stageChip = crmStageBadge(data.crm_status);

  return (
    <div className="px-8 py-7 max-w-4xl">
      <Link to={backTo} className="inline-flex items-center gap-1.5 text-[13px] text-slate-500 hover:text-white">
        <ArrowLeft className="w-4 h-4" /> Back to leads
      </Link>

      {/* Header */}
      <div className="mt-3 flex items-start justify-between flex-wrap gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-[26px] font-semibold text-white">
              {data.company.name || data.refined_company || "—"}
            </h1>
            <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px] ${badge.cls}`}>
              <span className={`h-1.5 w-1.5 rounded-full ${badge.dot}`} />
              {recommendationLabel(data.recommendation)}
            </span>
            <span
              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[12px] font-medium ${stageChip}`}
              title={`CRM stage: ${crmStageLabel(data.crm_status)}`}
            >
              {crmStageLabel(data.crm_status)}
            </span>
          </div>
          <p className="text-[13.5px] text-slate-500 mt-1">{data.email}</p>
          <p className="text-[13.5px] text-slate-500">
            {[data.company.industry, data.company.location].filter(Boolean).join(" · ") || "—"}
          </p>
        </div>
        <div className="text-right">
          <div className={`text-[34px] font-bold ${scoreColor(data.potential_score)}`}>
            {data.potential_score.toFixed(1)}
          </div>
          <div className="text-[12px] text-slate-500">potential score</div>
        </div>
      </div>

      {data.company.website && (
        <a
          href={data.company.website}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1.5 mt-3 text-[13.5px] text-indigo-400 hover:underline"
        >
          {data.company.website} <ExternalLink className="w-3.5 h-3.5" />
        </a>
      )}

      {/* CRM pipeline (Phase E1) — the lead's stage, next action, notes and
          the immutable timeline. Sits ABOVE the research sections: where the
          deal is comes before what the research found. */}
      <CrmPanel email={decoded} />

      {/* Fit summary — left-accent callout (the frontend1 design pattern):
          the AI's verdict reads as a quote, visually distinct from data. */}
      {data.fit && (
        <div className="mt-5 rounded-r-lg border-l-[3px] border-indigo-400/70 bg-indigo-500/[0.05] px-5 py-4">
          <h2 className="text-[11.5px] font-bold uppercase tracking-[0.14em] text-indigo-300/90 mb-1.5">
            Fit summary
          </h2>
          <p className="text-[13.5px] text-slate-100 leading-relaxed">{data.fit}</p>
        </div>
      )}

      {/* Sections */}
      <div className="mt-6 space-y-5">
        <Section title="Company" subtitle={data.refined_domain ? `domain: ${data.refined_domain}` : undefined}>
          <KV k="Industry" v={data.company.industry} />
          <KV k="Location" v={data.company.location} />
          {data.company.facts.length > 0 && <FactsList facts={data.company.facts} />}
        </Section>

        <Section title="Decision-maker">
          <KV k="Name" v={data.person.name} />
          <KV k="Role" v={data.person.role} />
          <KV k="Relevance" v={data.person.role_relevance ? "relevant role" : "role unclear"} />
          <KV k="Bound" v={data.person.bound ? "yes — reachable" : "not yet bound"} />
          {data.person.linkedin && <LinkedInLink url={data.person.linkedin} />}
          {data.person.evidence.length > 0 && <FactsList facts={data.person.evidence} />}
        </Section>

        <Section title="Intent / buying signal">
          {data.intent.signal && <KV k="Signal" v={data.intent.signal} />}
          <KV k="Estimate" v={data.intent.needs_estimation} />
          {data.intent.reason && (
            <ReasonProof
              label="Reason"
              reason={data.intent.reason}
              facts={data.intent.evidence}
            />
          )}
        </Section>

        <Section title="Timing">
          {data.timing.window && <KV k="Window" v={data.timing.window} />}
          {data.timing.reason && (
            <ReasonProof
              label="Reason"
              reason={data.timing.reason}
              facts={data.timing.events}
            />
          )}
        </Section>
      </div>

      {/* Sources audited */}
      {(data.sources_checked.length > 0 || Object.keys(data.source_errors).length > 0) && (
        <Section title="Sources audited">
          {data.sources_checked.length > 0 && (
            <ul className="flex flex-wrap gap-2">
              {data.sources_checked.map((s) => (
                <li key={s} className="rounded-md border border-white/5 bg-[#0B0E14] px-2 py-1 text-[12px] text-slate-200">
                  {s}
                </li>
              ))}
            </ul>
          )}
          {Object.entries(data.source_errors).map(([k, v]) => (
            <p key={k} className="text-[12px] text-amber-300/80 mt-2">
              {k}: {v}
            </p>
          ))}
        </Section>
      )}
    </div>
  );
}

function CrmPanel({ email }: { email: string }) {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["crm", email],
    queryFn: () => api.getCrm(email),
  });
  const save = useMutation({
    mutationFn: (input: CrmInput) => api.updateCrm(email, input),
    onSuccess: () => {
      // The stage/next-action chips live in three places — refresh them all.
      qc.invalidateQueries({ queryKey: ["crm", email] });
      qc.invalidateQueries({ queryKey: ["lead", email] });
      qc.invalidateQueries({ queryKey: ["leads"] });
    },
  });

  // Next-action draft: local edit until saved; resets when the server state
  // changes underneath (dirty stays true while the user is mid-edit).
  const [actionDraft, setActionDraft] = useState("");
  const [actionDirty, setActionDirty] = useState(false);
  useEffect(() => {
    if (!actionDirty) setActionDraft(data?.next_action ?? "");
  }, [data, actionDirty]);

  const [note, setNote] = useState("");
  const stage = data?.crm_status ?? "researched";
  const events = data?.events ?? [];

  return (
    <section className="rounded-xl border border-white/5 bg-white/[0.02] p-5">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-[14px] font-semibold text-white">
          Pipeline
          <span className="ml-2 font-normal text-slate-500">CRM stage · next action · notes</span>
        </h2>
        {save.isPending && (
          <span className="inline-flex items-center gap-1.5 text-[12px] text-slate-500">
            <Spinner className="h-3.5 w-3.5" /> saving…
          </span>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-4">
        <div>
          <span className="mb-1 block text-[11.5px] font-medium uppercase tracking-wide text-slate-500">Stage</span>
          <Select
            className="w-44"
            value={stage}
            onChange={(v) => save.mutate({ status: v })}
            disabled={save.isPending}
            ariaLabel="CRM stage"
            options={CRM_STAGES.map((s) => ({ value: s, label: crmStageLabel(s) }))}
          />
        </div>
        <div className="flex-1 min-w-[240px]">
          <span className="mb-1 block text-[11.5px] font-medium uppercase tracking-wide text-slate-500">Next action</span>
          <div className="flex gap-2">
            <input
              type="text"
              value={actionDraft}
              onChange={(e) => {
                setActionDraft(e.target.value);
                setActionDirty(true);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter" && actionDirty) {
                  save.mutate({ next_action: actionDraft });
                  setActionDirty(false);
                }
              }}
              placeholder="e.g. Send intro email — Friday"
              className="w-full rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-[13px] text-slate-200 placeholder:text-slate-600 outline-none focus:border-indigo-500/50"
            />
            {actionDirty && (
              <button
                onClick={() => {
                  save.mutate({ next_action: actionDraft });
                  setActionDirty(false);
                }}
                disabled={save.isPending}
                className="shrink-0 rounded-lg bg-indigo-600 px-3.5 py-2 text-[12.5px] font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                Save
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Note box — appended to the immutable timeline (never edited after). */}
      <div className="mt-3 flex gap-2">
        <input
          type="text"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && note.trim()) {
              save.mutate({ note: note.trim() });
              setNote("");
            }
          }}
          placeholder="Add a note to the timeline…"
          className="flex-1 rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2 text-[13px] text-slate-200 placeholder:text-slate-600 outline-none focus:border-indigo-500/50"
        />
        <button
          onClick={() => {
            if (!note.trim()) return;
            save.mutate({ note: note.trim() });
            setNote("");
          }}
          disabled={save.isPending || !note.trim()}
          className="shrink-0 rounded-lg border border-white/10 px-3.5 py-2 text-[12.5px] font-medium text-slate-300 hover:bg-white/[0.04] disabled:opacity-50"
        >
          Add note
        </button>
      </div>
      {save.isError && (
        <p className="mt-2 text-[12.5px] text-rose-300">
          {(save.error as Error).message}
        </p>
      )}

      {/* The timeline — newest last (chronological), like a conversation. */}
      {events.length > 0 && (
        <ul className="mt-4 space-y-2 border-t border-white/5 pt-3">
          {events.map((e) => (
            <CrmEventRow key={e.id} event={e} />
          ))}
        </ul>
      )}
    </section>
  );
}

function CrmEventRow({ event }: { event: CrmEvent }) {
  const icon =
    event.kind === "status" ? (
      <Flag className="h-3.5 w-3.5 text-indigo-300" />
    ) : event.kind === "next_action" ? (
      <CornerDownRight className="h-3.5 w-3.5 text-cyan-300" />
    ) : (
      <StickyNote className="h-3.5 w-3.5 text-amber-300" />
    );
  // Stage transitions ("researched → contacted") get the colored stage words;
  // everything else renders as plain text.
  const parts = event.detail.split(" → ");
  return (
    <li className="flex items-start gap-2 text-[13px]">
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        {event.kind === "status" && parts.length === 2 ? (
          <span>
            <span className={`rounded-full px-1.5 py-0.5 text-[11px] ${crmStageBadge(parts[0])}`}>
              {crmStageLabel(parts[0])}
            </span>{" "}
            <span className="text-slate-500">→</span>{" "}
            <span className={`rounded-full px-1.5 py-0.5 text-[11px] font-medium ${crmStageBadge(parts[1])}`}>
              {crmStageLabel(parts[1])}
            </span>
          </span>
        ) : (
          <span className="text-slate-100 break-words">{event.detail}</span>
        )}
        <span className="ml-2 text-[11.5px] text-slate-600">
          {event.username ? `${event.username} · ` : ""}
          {timeAgo(event.created_at)}
        </span>
      </div>
    </li>
  );
}

function ReasonProof({
  label,
  reason,
  facts,
}: {
  label: string;
  reason: string;
  facts: EvidenceFact[];
}) {
  const [open, setOpen] = useState(false);
  const links = facts.filter((f) => f.source_url);
  return (
    <div className="text-[13.5px]">
      <div className="flex gap-2">
        <span className="text-slate-500 w-28 shrink-0">{label}</span>
        <div className="flex-1 min-w-0">
          {/* Left-accent callout — the reason is the "why" of this section,
              not just another data row. */}
          <p className="text-slate-100 border-l-2 border-indigo-400/40 pl-3">
            {reason}
          </p>
          {links.length > 0 && (
            <button
              onClick={() => setOpen((v) => !v)}
              className="mt-1 text-[12px] text-indigo-400 hover:underline"
            >
              {open ? "▲ hide proof" : `▼ show proof (${links.length})`}
            </button>
          )}
          {open && (() => {
            // Dedupe by URL so many facts from the same page show the link once.
            const byUrl = new Map<string, string[]>();
            for (const f of links) {
              if (!byUrl.has(f.source_url)) byUrl.set(f.source_url, []);
              byUrl.get(f.source_url)!.push(f.claim);
            }
            return (
              <ul className="mt-2 flex flex-col gap-1.5 border-t border-white/5 pt-2">
                {Array.from(byUrl.entries()).map(([url, claims], i) => (
                  <li key={i}>
                    <a
                      href={url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 text-[12px] text-indigo-400 hover:underline break-all"
                    >
                      {url} <ExternalLink className="w-3 h-3 shrink-0" />
                    </a>
                    {claims.filter(Boolean).map((c, j) => (
                      <p key={j} className="text-[11px] text-slate-500 mt-0.5">{c}</p>
                    ))}
                  </li>
                ))}
              </ul>
            );
          })()}
        </div>
      </div>
    </div>
  );
}

function LinkedInLink({ url }: { url: string }) {
  const href = /^https?:\/\//i.test(url) ? url : `https://${url}`;
  return (
    <div className="flex gap-2 text-[13.5px] items-center">
      <span className="text-slate-500 w-28 shrink-0">LinkedIn</span>
      <a
        href={href}
        target="_blank"
        rel="noreferrer"
        className="inline-flex items-center gap-1 text-indigo-400 hover:underline break-all"
      >
        {url} <ExternalLink className="w-3 h-3 shrink-0" />
      </a>
    </div>
  );
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-white/5 bg-white/[0.02] p-5">
      <h2 className="text-[14px] font-semibold text-white">
        {title}
        {subtitle && <span className="ml-2 font-normal text-slate-500">{subtitle}</span>}
      </h2>
      <div className="mt-3 space-y-2">{children}</div>
    </section>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  if (!v) return null;
  return (
    <div className="flex gap-2 text-[13.5px]">
      <span className="text-slate-500 w-28 shrink-0">{k}</span>
      <span className="text-slate-100">{v}</span>
    </div>
  );
}

function FactsList({ facts }: { facts: EvidenceFact[] }) {
  if (facts.length === 0)
    return <p className="text-[12px] text-slate-500">No evidence recorded.</p>;

  // Group facts by source_url so a shared source (e.g. one LinkedIn profile)
  // shows its link ONCE with all claims beneath it — no per-line "▼ proof"
  // toggle repeating the same URL when many facts cite one page.
  const groups = new Map<string, EvidenceFact[]>();
  for (const f of facts) {
    const key = f.source_url || "(no source)";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(f);
  }

  return (
    <ul className="mt-3 space-y-3 border-t border-white/5 pt-3">
      {Array.from(groups.entries()).map(([url, group], gi) => {
        const hasUrl = url !== "(no source)";
        return (
          <li key={gi} className="text-[13.5px]">
            {hasUrl ? (
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-[12px] text-indigo-400 hover:underline break-all"
              >
                {url} <ExternalLink className="w-3 h-3 shrink-0" />
              </a>
            ) : (
              <span className="text-[12px] text-slate-500">No source</span>
            )}
            <ul className="mt-1.5 space-y-1.5 pl-3 border-l border-white/5">
              {group.map((f, j) => (
                <li key={j}>
                  <p className="text-slate-100">{f.claim}</p>
                  <div className="mt-0.5 flex items-center gap-2 text-[12px]">
                    <ConfidenceTag conf={f.confidence} />
                    <span className="text-slate-500">{f.source_type}</span>
                    {f.source_note && (
                      <span className="text-slate-400 text-[11px]">{f.source_note}</span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </li>
        );
      })}
    </ul>
  );
}

function ConfidenceTag({ conf }: { conf: string }) {
  const c = String(conf || "").toLowerCase();
  const cls = c.includes("high")
    ? "text-emerald-300 bg-emerald-500/15"
    : c.includes("medium")
      ? "text-amber-300 bg-amber-500/15"
      : "text-slate-300 bg-slate-500/15";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${cls}`}>{conf}</span>
  );
}
