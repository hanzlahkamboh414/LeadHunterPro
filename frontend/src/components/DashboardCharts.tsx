/**
 * DashboardCharts — the analytics layer of the Dashboard (Recharts).
 *
 * Every chart is a pure client-side projection of the LeadSummary list the
 * Dashboard already fetches (folder="*" universe) — no new backend endpoints,
 * so the numbers can never disagree with the lead list they summarize.
 *
 * All charts share the app's dark theme tokens (slate/indigo/emerald/amber)
 * so the analytics reads as one screen, not bolted-on widgets.
 */

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { LeadSummary } from "../types";

/** The shared card shell — same visual language as the stat cards. */
export const chartCardClass =
  "ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5";

/** Dark-theme tooltip styling (Recharts inline SVG — Tailwind can't reach it). */
const tooltipStyle = {
  backgroundColor: "rgba(15, 23, 42, 0.96)",
  border: "1px solid rgba(255, 255, 255, 0.08)",
  borderRadius: "8px",
  fontSize: "12px",
  color: "#e2e8f0",
} as const;

const REC_COLORS: Record<string, string> = {
  contact_now: "#34d399", // emerald-400
  nurture: "#fbbf24", // amber-400
  skip: "#94a3b8", // slate-400
};

const REC_LABELS: Record<string, string> = {
  contact_now: "Contact Now",
  nurture: "Nurture",
  skip: "Skip",
};

function dayLabel(day: string): string {
  return new Date(`${day}T00:00:00`).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

// ---------------------------------------------------------------------------
// Leads over time — total researched + working (contact_now) per day.
// ---------------------------------------------------------------------------

export function LeadsTrend({
  leads,
  days,
}: {
  leads: LeadSummary[];
  /** Trend window: 7 / 30 / 0 = all time. */
  days: number;
}) {
  // Count per research date (created_at is "YYYY-MM-DD" — the honest
  // extraction date, not a refresh timestamp).
  const byDay = new Map<string, { total: number; working: number }>();
  for (const l of leads) {
    const day = (l.created_at ?? "").slice(0, 10);
    if (!day) continue;
    const entry = byDay.get(day) ?? { total: 0, working: 0 };
    entry.total += 1;
    if (l.recommendation === "contact_now") entry.working += 1;
    byDay.set(day, entry);
  }

  // Build a CONTIGUOUS day axis — a missing day is a real 0, not a gap that
  // quietly connects two far-apart points. Window anchors on the newest data
  // day (or today when there is none yet), never a future date. "YYYY-MM-DD"
  // strings sort lexicographically = chronologically.
  const daysSorted = [...byDay.keys()].sort();
  const newest = byDay.size
    ? new Date(`${daysSorted[daysSorted.length - 1]}T00:00:00`)
    : new Date();
  const start = new Date(newest);
  if (days > 0) start.setDate(start.getDate() - (days - 1));
  const firstEver = byDay.size
    ? new Date(`${daysSorted[0]}T00:00:00`)
    : start;
  if (start < firstEver) start.setTime(firstEver.getTime());

  const data: { day: string; label: string; total: number; working: number }[] =
    [];
  for (const d = new Date(start); d <= newest; d.setDate(d.getDate() + 1)) {
    const key = d.toISOString().slice(0, 10);
    const entry = byDay.get(key);
    data.push({
      day: key,
      label: dayLabel(key),
      total: entry?.total ?? 0,
      working: entry?.working ?? 0,
    });
  }

  if (data.length === 0) return <ChartEmpty />;

  return (
    <ResponsiveContainer width="100%" height={260}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
        <defs>
          <linearGradient id="fillTotal" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#2dd4bf" stopOpacity={0.35} />
            <stop offset="100%" stopColor="#2dd4bf" stopOpacity={0.02} />
          </linearGradient>
          <linearGradient id="fillWorking" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#34d399" stopOpacity={0.3} />
            <stop offset="100%" stopColor="#34d399" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="rgba(255,255,255,0.05)" vertical={false} />
        <XAxis
          dataKey="label"
          tick={{ fill: "#64748b", fontSize: 11 }}
          axisLine={false}
          tickLine={false}
          minTickGap={28}
        />
        <YAxis
          allowDecimals={false}
          tick={{ fill: "#64748b", fontSize: 11 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip
          contentStyle={tooltipStyle}
          cursor={{ stroke: "rgba(129,140,248,0.3)" }}
        />
        <Area
          type="monotone"
          dataKey="total"
          name="Researched"
          stroke="#2dd4bf"
          strokeWidth={2}
          fill="url(#fillTotal)"
        />
        <Area
          type="monotone"
          dataKey="working"
          name="Contact Now"
          stroke="#34d399"
          strokeWidth={1.5}
          fill="url(#fillWorking)"
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Recommendation split — the quality funnel at a glance (donut).
// ---------------------------------------------------------------------------

export function RecommendationDonut({ leads }: { leads: LeadSummary[] }) {
  const counts = new Map<string, number>();
  for (const l of leads) {
    const key = l.recommendation in REC_LABELS ? l.recommendation : "skip";
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const data = ["contact_now", "nurture", "skip"].map((k) => ({
    name: REC_LABELS[k],
    key: k,
    value: counts.get(k) ?? 0,
  }));
  const total = leads.length;

  if (total === 0) return <ChartEmpty />;

  return (
    <div className="relative">
      <ResponsiveContainer width="100%" height={220}>
        <PieChart>
          <Pie
            data={data}
            dataKey="value"
            nameKey="name"
            innerRadius={62}
            outerRadius={88}
            paddingAngle={3}
            strokeWidth={0}
          >
            {data.map((d) => (
              <Cell key={d.key} fill={REC_COLORS[d.key]} />
            ))}
          </Pie>
          <Tooltip contentStyle={tooltipStyle} />
        </PieChart>
      </ResponsiveContainer>
      {/* Center total — the one number the donut is really about. */}
      <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
        <span className="text-[26px] font-semibold text-white leading-none">
          {total.toLocaleString()}
        </span>
        <span className="text-[11px] text-slate-500 mt-1">leads</span>
      </div>
      {/* Legend with honest counts */}
      <div className="flex items-center justify-center gap-4 mt-1">
        {data.map((d) => (
          <span key={d.key} className="flex items-center gap-1.5 text-[12px] text-slate-400">
            <span
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: REC_COLORS[d.key] }}
            />
            {d.name} <span className="text-slate-500">{d.value.toLocaleString()}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Score distribution — 0–10 potential in 2-point buckets.
// ---------------------------------------------------------------------------

const SCORE_BUCKETS = ["0–2", "2–4", "4–6", "6–8", "8–10"];

function bucketOf(score: number): number {
  if (!Number.isFinite(score) || score <= 0) return 0;
  return Math.min(4, Math.floor(score / 2));
}

export function ScoreHistogram({ leads }: { leads: LeadSummary[] }) {
  const counts = [0, 0, 0, 0, 0];
  for (const l of leads) counts[bucketOf(l.score)] += 1;
  const data = SCORE_BUCKETS.map((bucket, i) => ({
    bucket,
    leads: counts[i],
    // Bars colored by quality band — same semantics as scoreColor().
    fill: i >= 3 ? "#34d399" : i === 2 ? "#fbbf24" : "#64748b",
  }));

  if (leads.length === 0) return <ChartEmpty />;

  return (
    <ResponsiveContainer width="100%" height={220}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
        <CartesianGrid stroke="rgba(255,255,255,0.05)" vertical={false} />
        <XAxis
          dataKey="bucket"
          tick={{ fill: "#64748b", fontSize: 11 }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          allowDecimals={false}
          tick={{ fill: "#64748b", fontSize: 11 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
        <Bar dataKey="leads" name="Leads" radius={[4, 4, 0, 0]}>
          {data.map((d) => (
            <Cell key={d.bucket} fill={d.fill} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ---------------------------------------------------------------------------
// Top sources — which trade · location runs produced the most leads.
// ---------------------------------------------------------------------------

export function TopSources({
  leads,
  onPick,
}: {
  leads: LeadSummary[];
  onPick?: (source: string) => void;
}) {
  const counts = new Map<string, number>();
  for (const l of leads) {
    const src = l.source?.trim();
    if (src) counts.set(src, (counts.get(src) ?? 0) + 1);
  }
  const data = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([source, leads]) => ({ source, leads: leads }))
    .reverse(); // vertical bars render bottom-up — reverse so #1 is on top

  if (data.length === 0) return <ChartEmpty />;

  return (
    <ResponsiveContainer width="100%" height={Math.max(160, data.length * 34)}>
      <BarChart
        data={data}
        layout="vertical"
        margin={{ top: 0, right: 30, left: 0, bottom: 0 }}
      >
        <XAxis type="number" hide allowDecimals={false} />
        <YAxis
          type="category"
          dataKey="source"
          width={190}
          tick={{ fill: "#94a3b8", fontSize: 11 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
        <Bar
          dataKey="leads"
          name="Leads"
          fill="#2dd4bf"
          radius={[0, 4, 4, 0]}
          barSize={16}
          // Recharts hands the clicked BarRectangleItem; the datum lives in
          // its `payload` — unwrap defensively, never assume the shape.
          onClick={(data: unknown) => {
            const src = (data as { payload?: { source?: string } })?.payload
              ?.source;
            if (src) onPick?.(src);
          }}
          className={onPick ? "cursor-pointer" : ""}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

function ChartEmpty() {
  return (
    <div className="flex items-center justify-center h-[220px] text-[13px] text-slate-500">
      No data yet — run a search first.
    </div>
  );
}
