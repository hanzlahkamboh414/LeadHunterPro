// Small presentational helpers shared across screens.

export type Recommendation = "contact_now" | "nurture" | "skip";

export function isRecommendation(v: string): v is Recommendation {
  return v === "contact_now" || v === "nurture" || v === "skip";
}

const REC_LABEL: Record<Recommendation, string> = {
  contact_now: "Contact Now",
  nurture: "Nurture",
  skip: "Skip",
};

const REC_BADGE: Record<
  Recommendation,
  { cls: string; dot: string }
> = {
  contact_now: {
    cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
    dot: "bg-emerald-400",
  },
  nurture: {
    cls: "bg-amber-500/15 text-amber-300 border-amber-500/40",
    dot: "bg-amber-400",
  },
  skip: {
    cls: "bg-slate-500/15 text-slate-300 border-slate-500/40",
    dot: "bg-slate-400",
  },
};

export function recommendationLabel(v: string): string {
  return isRecommendation(v) ? REC_LABEL[v] : v;
}

export function recommendationBadge(v: string): { cls: string; dot: string } {
  return isRecommendation(v)
    ? REC_BADGE[v]
    : REC_BADGE.skip;
}

export const STATE_LABEL: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  paused: "Paused",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function elapsed(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m <= 0) return `${s}s`;
  return `${m}m ${s}s`;
}

// The backend stores UTC timestamps WITHOUT a timezone suffix — e.g.
// "2026-09-05T16:05:49" (jobs.py `_now()` uses strftime with no 'Z'). Browsers
// parse a suffix-less ISO string as LOCAL time, which shifts every timestamp by
// the machine's UTC offset: on a +5h (PKT) box a job created 10 minutes ago
// rendered as "5h ago". Treat suffix-less timestamps as UTC so times are honest.
const TZ_SUFFIX = /(Z|[+-]\d{2}:?\d{2})$/;

export function parseUtc(iso: string): number {
  const withZone = TZ_SUFFIX.test(iso) ? iso : `${iso}Z`;
  return new Date(withZone).getTime();
}

export function timeAgo(iso: string | undefined): string {
  if (!iso) return "—";
  const then = parseUtc(iso);
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export function formatTime(iso: string | undefined): string {
  if (!iso) return "—";
  const d = new Date(parseUtc(iso));
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function scoreColor(score: number): string {
  if (score >= 7) return "text-emerald-300";
  if (score >= 4) return "text-amber-300";
  return "text-slate-400";
}

// CRM pipeline stages (Phase E1) — the lead's journey chip. Labels are the
// user's own vocabulary; colours are LITERAL class strings (Tailwind JIT only
// generates classes it sees in source — never build them at runtime).
export const CRM_STAGE_LABEL: Record<string, string> = {
  new: "New",
  researched: "Researched",
  qualified: "Qualified",
  contacted: "Contacted",
  opened: "Opened",
  replied: "Replied",
  interested: "Interested",
  meeting: "Meeting",
  won: "Won",
  lost: "Lost",
};

const CRM_STAGE_BADGE: Record<string, string> = {
  new: "bg-slate-500/15 text-slate-300",
  researched: "bg-sky-500/15 text-sky-300",
  qualified: "bg-indigo-500/15 text-indigo-300",
  contacted: "bg-violet-500/15 text-violet-300",
  opened: "bg-cyan-500/15 text-cyan-300",
  replied: "bg-teal-500/15 text-teal-300",
  interested: "bg-emerald-500/15 text-emerald-300",
  meeting: "bg-amber-500/15 text-amber-300",
  won: "bg-emerald-500/25 text-emerald-200 border border-emerald-400/40",
  lost: "bg-rose-500/15 text-rose-300",
};

export function crmStageLabel(v: string): string {
  return CRM_STAGE_LABEL[v] ?? v;
}

export function crmStageBadge(v: string): string {
  return CRM_STAGE_BADGE[v] ?? CRM_STAGE_BADGE.researched;
}
