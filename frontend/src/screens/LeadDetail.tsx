import { useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Activity, ArrowLeft, BrainCircuit, ChevronDown, ChevronUp, ExternalLink, Sparkles, Target } from "lucide-react";
import { api } from "../api/client";
import type { CompanyIntelligence as CompanyIntelligenceData, EvidenceFact } from "../types";
import { CopyButton } from "../components/CopyButton";
import { Spinner } from "../components/StatusChip";
import { recommendationBadge, recommendationLabel, scoreColor } from "../lib/format";

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

  return (
    <div className="workspace-page page-focused company-dossier">
      <Link to={backTo} className="inline-flex items-center gap-1.5 text-[13px] text-slate-500 hover:text-white">
        <ArrowLeft className="w-4 h-4" /> Back to leads
      </Link>

      {/* Header */}
      <div className="dossier-hero mt-3 flex items-start justify-between flex-wrap gap-4">
        <div className="min-w-0 flex-1">
          <p className="page-eyebrow mb-4">Company research</p>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-[26px] font-semibold text-white">
              {data.company.name || data.refined_company || "—"}
            </h1>
            <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[12px] ${badge.cls}`}>
              <span className={`h-1.5 w-1.5 rounded-full ${badge.dot}`} />
              {recommendationLabel(data.recommendation)}
            </span>
          </div>
          <p className="break-all text-[13.5px] text-slate-500 mt-3 inline-flex items-center gap-1.5">
            {data.email}
            <CopyButton value={data.email} label="email" />
          </p>
          <p className="text-[13.5px] text-slate-500">
            {[data.company.industry, data.company.location].filter(Boolean).join(" · ") || "—"}
          </p>
        </div>
        <div className="dossier-score text-center">
          <div className={`text-[34px] font-bold ${scoreColor(data.potential_score)}`}>
            {data.potential_score.toFixed(1)}
          </div>
          <div className="text-[12px] text-slate-400">Potential / 10</div>
        </div>
      </div>

      {data.company.website && (
        <a
          href={data.company.website}
          target="_blank"
          rel="noreferrer"
          className="break-all inline-flex items-center gap-1.5 mt-3 text-[13.5px] text-indigo-400 hover:underline"
        >
          {data.company.website} <ExternalLink className="w-3.5 h-3.5" />
        </a>
      )}

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

      {data.signal_intelligence?.company_id && (
        <CompanyIntelligence intelligence={data.signal_intelligence} />
      )}

      {/* Sections */}
      <div className="dossier-sections mt-6">
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

function CompanyIntelligence({ intelligence }: { intelligence: CompanyIntelligenceData }) {
  const activity = intelligence.recent_activity || [];
  const signals = intelligence.signals || [];
  const pains = intelligence.pain_hypotheses || [];
  const trigger = intelligence.outreach_trigger;
  return (
    <section className="mt-6 overflow-hidden rounded-2xl border border-indigo-400/20 bg-gradient-to-br from-indigo-500/[0.08] via-white/[0.025] to-cyan-500/[0.04]">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-white/[0.07] px-5 py-4">
        <div>
          <div className="flex items-center gap-2 text-indigo-300">
            <BrainCircuit className="h-4 w-4" />
            <h2 className="text-[14px] font-semibold text-white">Company Intelligence</h2>
          </div>
          <p className="mt-1 text-[12px] text-slate-500">Current company activity, evidence and safe outreach guidance</p>
        </div>
        <span className="rounded-full border border-indigo-400/20 bg-indigo-500/10 px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-indigo-200">
          evidence licensed
        </span>
      </div>

      <div className="grid gap-px bg-white/[0.06] md:grid-cols-3">
        <IntelColumn icon={<Activity className="h-4 w-4" />} title="Recent activity" count={activity.length}>
          {activity.length ? activity.map((item) => (
            <div key={item.event_id} className="rounded-lg border border-white/[0.06] bg-black/10 p-3">
              <p className="text-[12.5px] font-medium text-slate-100">{pretty(item.event_type)}</p>
              <p className="mt-1 text-[11px] text-slate-500">{displayDate(item.occurred_at)} · {pretty(item.recency)}</p>
            </div>
          )) : <EmptyIntel text="No activity verified in the last 90 days." />}
        </IntelColumn>

        <IntelColumn icon={<Sparkles className="h-4 w-4" />} title="Signals" count={signals.length}>
          {signals.length ? signals.map((signal) => (
            <div key={signal.signal_id} className="rounded-lg border border-white/[0.06] bg-black/10 p-3">
              <div className="flex items-start justify-between gap-2">
                <p className="text-[12.5px] font-medium text-slate-100">{pretty(signal.signal_type)}</p>
                <Strength value={signal.strength} />
              </div>
              <p className="mt-1 text-[11px] text-slate-500">{signal.event_count} event{signal.event_count === 1 ? "" : "s"} · {signal.independent_source_count} source{signal.independent_source_count === 1 ? "" : "s"}</p>
            </div>
          )) : <EmptyIntel text="No current signal reached the scoring threshold." />}
        </IntelColumn>

        <IntelColumn icon={<Target className="h-4 w-4" />} title="Recommended angle" count={trigger ? 1 : 0}>
          <div className="rounded-lg border border-indigo-400/15 bg-indigo-500/[0.07] p-3">
            <p className="text-[12.5px] font-semibold text-indigo-200">{intelligence.recommended_angle || "General Estimating Support"}</p>
            {trigger && <>
              <div className="mt-2"><Strength value={trigger.strength} /></div>
              <p className="mt-2 text-[12px] leading-relaxed text-slate-300">“{trigger.wording}”</p>
            </>}
          </div>
        </IntelColumn>
      </div>

      <div className="border-t border-white/[0.07] px-5 py-4">
        <h3 className="text-[12px] font-semibold uppercase tracking-[0.12em] text-slate-400">Pain hypotheses</h3>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {pains.length ? pains.map((pain) => <PainCard key={pain.hypothesis_id} pain={pain} />) : (
            <EmptyIntel text="Unknown — no evidence-backed pain hypothesis is available." />
          )}
        </div>
      </div>
    </section>
  );
}

function IntelColumn({ icon, title, count, children }: { icon: React.ReactNode; title: string; count: number; children: React.ReactNode }) {
  return <div className="min-w-0 bg-[#0d111a]/90 p-4">
    <div className="mb-3 flex items-center gap-2 text-slate-400">{icon}<h3 className="text-[11px] font-bold uppercase tracking-[0.12em]">{title}</h3><span className="ml-auto text-[10px] text-slate-600">{count}</span></div>
    <div className="space-y-2">{children}</div>
  </div>;
}

function PainCard({ pain }: { pain: CompanyIntelligenceData["pain_hypotheses"][number] }) {
  const [open, setOpen] = useState(false);
  const licensed = (pain.verdict === "VERIFIED" || pain.verdict === "LIKELY") && pain.why.length > 0;
  const verdict = licensed ? pain.verdict : pain.verdict === "BLOCKED" ? "BLOCKED" : "UNKNOWN";
  return <div className="rounded-xl border border-white/[0.07] bg-black/10 p-3.5">
    <div className="flex items-start justify-between gap-3">
      <div><p className="text-[13px] font-medium text-slate-100">{pretty(pain.pain_type)}</p><p className="mt-1 text-[11.5px] leading-relaxed text-slate-400">{pain.reasoning || "No verified explanation available."}</p></div>
      <Strength value={verdict} />
    </div>
    {licensed && <p className="mt-2 text-[11px] text-slate-500">Confidence {Math.round(pain.confidence * 100)}%</p>}
    <button onClick={() => setOpen((value) => !value)} className="mt-2 inline-flex items-center gap-1 text-[11.5px] font-medium text-indigo-300 hover:text-indigo-200">
      Why? {open ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
    </button>
    {open && <div className="mt-2 space-y-2 border-t border-white/[0.06] pt-2">
      {pain.blocked_by.map((reason) => <p key={reason} className="text-[11px] text-amber-200/75">Blocked: {reason}</p>)}
      {pain.why.length ? pain.why.map((item) => <a key={item.evidence_id} href={item.source_url} target="_blank" rel="noreferrer" className="block rounded-lg border border-white/[0.05] bg-white/[0.02] p-2.5 hover:border-indigo-400/20">
        <div className="flex items-center justify-between gap-2"><span className="text-[11px] font-medium text-indigo-300">{item.publisher || item.source_type || "Source"}</span><span className="text-[10px] text-slate-600">{displayDate(item.published_at)}</span></div>
        {item.title && <p className="mt-1 text-[11.5px] text-slate-200">{item.title}</p>}
        {item.excerpt && <p className="mt-1 line-clamp-3 text-[11px] leading-relaxed text-slate-500">{item.excerpt}</p>}
      </a>) : <p className="text-[11px] text-slate-500">Unknown — no supporting source is attached.</p>}
    </div>}
  </div>;
}

function Strength({ value }: { value: string }) {
  const normalized = String(value || "UNKNOWN").toUpperCase();
  const cls = normalized === "CONFIRMED" || normalized === "VERIFIED" || normalized === "HIGH"
    ? "border-emerald-400/20 bg-emerald-500/10 text-emerald-300"
    : normalized === "LIKELY" || normalized === "STRONG_INFERENCE" || normalized === "MEDIUM"
      ? "border-cyan-400/20 bg-cyan-500/10 text-cyan-300"
      : normalized === "WEAK_INFERENCE" || normalized === "LOW"
        ? "border-amber-400/20 bg-amber-500/10 text-amber-300"
        : "border-slate-500/20 bg-slate-500/10 text-slate-400";
  return <span className={`inline-flex shrink-0 rounded-full border px-2 py-0.5 text-[9.5px] font-bold uppercase tracking-wide ${cls}`}>{pretty(normalized)}</span>;
}

function EmptyIntel({ text }: { text: string }) { return <p className="rounded-lg border border-dashed border-white/[0.08] p-3 text-[11.5px] leading-relaxed text-slate-500">{text}</p>; }
function pretty(value: string) { return String(value || "Unknown").split("_").join(" ").toLowerCase().replace(/\b\w/g, (c: string) => c.toUpperCase()); }
function displayDate(value: string) { if (!value) return "Date unknown"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date); }

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
      <CopyButton value={url} label="LinkedIn URL" />
    </div>
  );
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section className="dossier-section ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5">
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
    <div className="dossier-fact flex gap-2 text-[13.5px]">
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
