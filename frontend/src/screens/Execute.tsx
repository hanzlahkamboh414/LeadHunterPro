import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Rocket, KeyRound } from "lucide-react";
import { api, ApiError, getStoredApiKey, setStoredApiKey } from "../api/client";
import type { Job } from "../types";
import StatusChip, { Spinner } from "../components/StatusChip";
import { elapsed, recommendationBadge, recommendationLabel } from "../lib/format";

export default function Execute() {
  const [trade, setTrade] = useState("");
  const [location, setLocation] = useState("");
  const [targetEmails, setTargetEmails] = useState(10);
  const [searchName, setSearchName] = useState("");
  const [saveFolder, setSaveFolder] = useState("");
  const [apiKey, setApiKey] = useState(getStoredApiKey());
  const [showKey, setShowKey] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api.createJob({
        trade,
        location,
        target_emails: targetEmails,
        search_name: searchName.trim() || undefined,
        folder: saveFolder.trim() || undefined,
      }),
    onSuccess: (job) => setJobId(job.id),
  });

  // Existing folders (datalist options) so the user can pick an EXISTING folder
  // or type a brand-new one — leads land filed, never mixed.
  const folders = useQuery({
    queryKey: ["folders"],
    queryFn: () => api.listFolders(),
    enabled: true,
  });
  const folderOptions = folders.data?.folders.map((f) => f.name) ?? [];

  // Poll the job while it is queued/running, stop once terminal. retry:true
  // keeps polling through a backend restart so a temporarily unreachable server
  // can't strand the view in a stale "running" state.
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId!),
    enabled: !!jobId,
    retry: true,
    retryDelay: 2000,
    refetchInterval: (q) => {
      const s = q.state.data?.state;
      return s === "running" || s === "queued" ? 1500 : false;
    },
  });

  const cancel = useMutation({ mutationFn: () => api.cancelJob(jobId!) });
  const pause = useMutation({ mutationFn: () => api.pauseJob(jobId!) });
  const qc = useQueryClient();
  // Continue (failed/interrupted) flips the job back to running — refetch
  // immediately so the live poll re-engages from the new running state.
  const resume = useMutation({
    mutationFn: () => api.resumeJob(jobId!),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["job", jobId] }),
  });

  const active = job.data?.state === "running" || job.data?.state === "queued";
  const paused = job.data?.state === "paused";

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (create.isPending) return;
    create.mutate();
  }

  function saveKey() {
    setStoredApiKey(apiKey.trim());
  }

  return (
    <div className="px-8 py-7 max-w-4xl">
      <h1 className="text-[26px] font-semibold text-white">Research</h1>
      <p className="text-slate-500 text-[13.5px] mt-1">
        Discovery → AI research → qualified leads, run as a background job you can watch live.
      </p>

      {/* API key (M12 baseline) */}
      <div className="mt-6 rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <KeyRound className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div className="flex-1">
            <label className="block text-[13px] text-slate-300">
              API key{" "}
              <span className="text-slate-500">(only if the backend sets LEADS_API_KEY)</span>
            </label>
          </div>
          <button
            onClick={() => setShowKey((v) => !v)}
            className="text-[12px] text-slate-500 hover:text-white"
          >
            {showKey ? "Hide" : "Show"}
          </button>
        </div>
        <div className="mt-3 flex gap-2">
          <input
            type={showKey ? "text" : "password"}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            onBlur={saveKey}
            placeholder="Leave blank if no key is configured"
            className="flex-1 bg-white/[0.04] border border-white/5 rounded-lg px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40"
          />
          <button
            onClick={saveKey}
            className="rounded-lg border border-white/5 px-4 text-[13px] text-slate-300 hover:bg-white/[0.04]"
          >
            Save
          </button>
        </div>
      </div>

      {/* Query form */}
      <div className="mt-5 rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Field label="Trade (WHAT)">
              <input
                required
                value={trade}
                onChange={(e) => setTrade(e.target.value)}
                placeholder="e.g. general contractor"
                className={inputCls}
              />
            </Field>
            <Field label="Location (WHERE)">
              <input
                required
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                placeholder="e.g. Houston TX, Dallas, Austin, Texas"
                className={inputCls}
              />
              <p className="text-[11.5px] text-slate-600 mt-1.5">
                City-level search yields more targeted results (e.g. "Houston TX" or "Dallas County")
              </p>
              <div className="flex flex-wrap gap-1.5 mt-2">
                {CITY_SUGGESTIONS.map((city) => (
                  <button
                    key={city}
                    type="button"
                    onClick={() => setLocation(city)}
                    className={`rounded-md px-2 py-0.5 text-[11.5px] border transition-colors ${
                      location === city
                        ? "border-indigo-500/50 bg-indigo-500/10 text-indigo-300"
                        : "border-white/5 bg-white/[0.02] text-slate-500 hover:text-slate-300 hover:bg-white/[0.04]"
                    }`}
                  >
                    {city}
                  </button>
                ))}
              </div>
            </Field>
          </div>
          <Field label="Target number of leads">
            <input
              type="number"
              min={1}
              max={100}
              value={targetEmails}
              onChange={(e) => setTargetEmails(Number(e.target.value))}
              className={`${inputCls} max-w-[180px]`}
            />
          </Field>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-1">
            <Field label="Search name (optional — tag this run)">
              <input
                value={searchName}
                onChange={(e) => setSearchName(e.target.value)}
                placeholder='e.g. "Houston GC Q3" — every lead gets this tag'
                className={inputCls}
              />
              <p className="text-[11.5px] text-slate-600 mt-1.5">
                Label so a later search's leads never mix with this one.
              </p>
            </Field>
            <Field label="Save to folder (optional)">
              <input
                value={saveFolder}
                onChange={(e) => setSaveFolder(e.target.value)}
                placeholder='e.g. "Q3 Outreach" or pick existing'
                list="execute-folder-options"
                className={inputCls}
              />
              <datalist id="execute-folder-options">
                {folderOptions.map((f) => (
                  <option key={f} value={f} />
                ))}
              </datalist>
              <p className="text-[11.5px] text-slate-600 mt-1.5">
                Every result is auto-filed here as it's found — stays out of the inbox.
              </p>
            </Field>
          </div>

          {create.isError && (
            <p className="text-[13px] text-rose-300">
              Failed to start job: {(create.error as ApiError).message}
            </p>
          )}

          <div className="flex items-center gap-3 pt-1">
            <button
              type="submit"
              disabled={create.isPending || !!jobId}
              className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-5 py-2.5 text-[13.5px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {create.isPending ? <Spinner /> : <Rocket className="w-4 h-4" />} Execute Search
            </button>
            {jobId && (
              <button
                type="button"
                onClick={() => {
                  setJobId(null);
                  create.reset();
                }}
                className="text-[13px] text-slate-500 hover:text-white"
              >
                Start another
              </button>
            )}
          </div>
        </form>
      </div>

      {/* Backend restart / server blip: keep watching, this run is NOT lost. */}
      {job.isError && (
        <div className="mt-5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3">
          <p className="text-[13px] text-amber-300">
            ⏳ Backend unreachable — reconnecting… your run{" "}
            {job.data?.results?.length ? "keeps its results so far and " : ""}
            resumes automatically once the server is back.
          </p>
        </div>
      )}

      {job.data && (
        <JobLiveView
          job={job.data}
          running={active}
          paused={paused}
          onCancel={() => cancel.mutate()}
          onPause={() => pause.mutate()}
          onResume={() => resume.mutate()}
          cancelPending={cancel.isPending}
          pausePending={pause.isPending}
          resumePending={resume.isPending}
        />
      )}
    </div>
  );
}

function JobLiveView({
  job,
  running,
  paused,
  onCancel,
  onPause,
  onResume,
  cancelPending,
  pausePending,
  resumePending,
}: {
  job: Job;
  running: boolean;
  paused: boolean;
  onCancel: () => void;
  onPause: () => void;
  onResume: () => void;
  cancelPending: boolean;
  pausePending: boolean;
  resumePending: boolean;
}) {
  const navigate = useNavigate();
  const done = job.state === "completed" || job.state === "failed" || job.state === "cancelled";
  const newest = job.events[job.events.length - 1];
  const eta = estimateRemaining(job);

  return (
    <div className="mt-6 rounded-xl border border-white/5 bg-white/[0.02] p-5">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div className="flex items-center gap-3">
            <h2 className="text-[17px] font-semibold text-white">Job {job.id.slice(0, 8)}</h2>
            <StatusChip state={job.state} />
            {eta && (running || paused) && (
              <span className="text-[12px] text-slate-400">⏱ est. {eta}</span>
            )}
          </div>
          <p className="text-[13px] text-slate-500 mt-1">
            {job.query.trade} · {job.query.location} · target {job.query.target_emails} ·{" "}
            {elapsed(job.elapsed_s)} elapsed
          </p>
          {(job.query.search_name || job.query.folder) && (
            <p className="text-[12.5px] text-indigo-300/90 mt-1">
              {job.query.folder && <>→ auto-filing into <span className="font-medium">“{job.query.folder}”</span></>}
              {job.query.folder && job.query.search_name && " · "}
              {job.query.search_name && <>tagged <span className="font-medium">“{job.query.search_name}”</span></>}
              {" "}— stays out of the unfiled inbox.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {running && (
            <>
              <button
                onClick={onPause}
                disabled={pausePending}
                className="rounded-lg border border-amber-500/40 px-3 py-1.5 text-[13px] text-amber-300 hover:bg-amber-500/10 disabled:opacity-50"
              >
                {pausePending ? "Pausing…" : "⏸ Pause"}
              </button>
              <button
                onClick={onCancel}
                disabled={cancelPending}
                className="rounded-lg border border-rose-500/40 px-3 py-1.5 text-[13px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
              >
                {cancelPending ? "Cancelling…" : "Cancel"}
              </button>
            </>
          )}
          {paused && (
            <>
              <button
                onClick={onResume}
                disabled={resumePending}
                className="rounded-lg bg-indigo-600 px-3 py-1.5 text-[13px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {resumePending ? "Resuming…" : "▶ Resume"}
              </button>
              <button
                onClick={onCancel}
                disabled={cancelPending}
                className="rounded-lg border border-rose-500/40 px-3 py-1.5 text-[13px] text-rose-300 hover:bg-rose-500/10 disabled:opacity-50"
              >
                {cancelPending ? "Cancelling…" : "Cancel"}
              </button>
            </>
          )}
          {job.state === "failed" && (
            <>
              <button
                onClick={onResume}
                disabled={resumePending}
                className="rounded-lg bg-indigo-600 px-3 py-1.5 text-[13px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {resumePending ? "Continuing…" : "▶ Continue Search"}
              </button>
              {job.results.length > 0 && (
                <button
                  onClick={() => navigate("/leads")}
                  className="rounded-lg border border-white/10 px-3 py-1.5 text-[13px] text-slate-300 hover:bg-white/5"
                >
                  View {job.results.length} lead{job.results.length === 1 ? "" : "s"} so far →
                </button>
              )}
            </>
          )}
          {done && job.state !== "failed" && job.results.length > 0 && (
            <button
              onClick={() => navigate("/leads")}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-[13px] font-semibold text-white hover:bg-indigo-500"
            >
              View leads →
            </button>
          )}
        </div>
      </div>

      {job.error && (
        <p className="mt-3 text-[13px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2">{job.error}</p>
      )}

      {/* Progress bar */}
      <div className="mt-4">
        <p className="text-[12px] text-slate-500 mb-1">
          {newest ? `${newest.phase} — step ${newest.step}/${newest.total}` : "Waiting to start…"}
        </p>
        <div className="h-2 rounded-full bg-[#0B0E14] overflow-hidden">
          <div className="h-full bg-indigo-500 transition-all" style={{ width: progressWidth(newest) }} />
        </div>
      </div>

      <div className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Results so far */}
        <div>
          <h3 className="text-[12px] font-semibold uppercase tracking-wide text-slate-500 mb-2">
            Leads ({job.results.length})
          </h3>
          {job.results.length === 0 ? (
            <p className="text-[13px] text-slate-500">No leads yet.</p>
          ) : (
            <ul className="space-y-2 max-h-80 overflow-y-auto pr-1">
              {job.results.map((r) => (
                <li key={r.email} className="rounded-lg border border-white/5 bg-[#0B0E14]/50 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[13.5px] font-medium text-white truncate">{r.company}</span>
                    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] ${recommendationBadge(r.recommendation).cls}`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${recommendationBadge(r.recommendation).dot}`} />
                      {recommendationLabel(r.recommendation)}
                    </span>
                  </div>
                  <p className="text-[12px] text-slate-500 truncate mt-0.5">{r.email}</p>
                  {r.error && <p className="text-[12px] text-rose-300 mt-1">{r.error}</p>}
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Event log */}
        <div>
          <h3 className="text-[12px] font-semibold uppercase tracking-wide text-slate-500 mb-2">
            Progress log ({job.events.length})
          </h3>
          {job.events.length === 0 ? (
            <p className="text-[13px] text-slate-500">No events yet.</p>
          ) : (
            <ul className="space-y-1 max-h-80 overflow-y-auto pr-1 font-mono text-[12px]">
              {job.events.map((ev, i) => (
                <li key={i} className="flex gap-2 text-slate-500">
                  <span className="text-slate-600 shrink-0">{new Date(ev.ts).toLocaleTimeString()}</span>
                  <span className={ev.email ? "text-sky-300" : "text-slate-200"}>
                    {ev.phase}: {ev.message}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

function progressWidth(ev: { step: number; total: number } | undefined): string {
  if (!ev || !ev.total) return "0%";
  return `${Math.min(100, Math.round((ev.step / ev.total) * 100))}%`;
}

// Rough ETA from live telemetry: extrapolate the current phase's step/total
// against total elapsed time. Honest estimate, not a promise.
function estimateRemaining(job: Job): string | null {
  const ev = job.events[job.events.length - 1];
  if (!ev || !ev.total || !ev.step || !job.elapsed_s) return null;
  const frac = ev.step / ev.total;
  if (frac <= 0) return null;
  const total = job.elapsed_s / frac;
  const rem = total - job.elapsed_s;
  if (rem <= 0) return "finishing…";
  const m = Math.floor(rem / 60);
  const s = Math.round(rem % 60);
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

const CITY_SUGGESTIONS = [
  "Houston TX",
  "Dallas TX",
  "Austin TX",
  "San Antonio TX",
  "Fort Worth TX",
  "Miami FL",
  "Orlando FL",
  "Tampa FL",
];

const inputCls =
  "w-full bg-white/[0.04] border border-white/5 rounded-lg px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40";

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block text-[13px] text-slate-300 mb-1.5">{label}</span>
      {children}
    </label>
  );
}
