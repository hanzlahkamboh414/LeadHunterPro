import { STATE_LABEL } from "../lib/format";

const STYLES: Record<string, string> = {
  queued: "bg-slate-500/15 text-slate-300 border-white/5",
  running: "bg-sky-500/15 text-sky-300 border-white/5 animate-pulse",
  paused: "bg-amber-500/15 text-amber-300 border-white/5",
  completed: "bg-emerald-500/15 text-emerald-300 border-white/5",
  failed: "bg-rose-500/15 text-rose-300 border-white/5",
  cancelled: "bg-slate-500/15 text-slate-300 border-white/5",
};

export default function StatusChip({ state }: { state: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${
        STYLES[state] ?? STYLES.queued
      }`}
    >
      {state === "running" && <Spinner className="h-3 w-3" />}
      {STATE_LABEL[state] ?? state}
    </span>
  );
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      className={`animate-spin ${className}`}
      viewBox="0 0 24 24"
      fill="none"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
      />
    </svg>
  );
}
