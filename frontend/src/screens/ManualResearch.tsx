import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type LeadImportJob } from "../api/client";

const activeStatus = (status: string) => ["queued", "running", "waiting_quota"].includes(status);
const errorText = (error: unknown) => error instanceof ApiError ? error.message : "Please try again.";

export default function ManualResearch({ aiEnabled = true }: { aiEnabled?: boolean }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [fileError, setFileError] = useState("");
  const jobs = useQuery({ queryKey: ["lead-imports"], queryFn: api.listLeadImports, refetchInterval: 3000 });
  const quota = useQuery({ queryKey: ["lead-import-quota"], queryFn: api.leadImportQuota, refetchInterval: 10000 });
  useEffect(() => {
    if (jobs.data && (!selected || !jobs.data.some((job) => job.id === selected))) setSelected(jobs.data[0]?.id ?? null);
  }, [jobs.data, selected]);
  const current = jobs.data?.find((job) => job.id === selected);
  const activity = useQuery({ queryKey: ["lead-import-activity", selected], queryFn: () => api.leadImportActivity(selected!), enabled: !!selected, refetchInterval: current && activeStatus(current.status) ? 5000 : false });
  function refresh() { qc.invalidateQueries({ queryKey: ["lead-imports"] }); qc.invalidateQueries({ queryKey: ["lead-import-quota"] }); qc.invalidateQueries({ queryKey: ["lead-import-activity"] }); qc.invalidateQueries({ queryKey: ["leads"] }); }
  const upload = useMutation({ mutationFn: () => api.uploadLeadFile(file!, name), onSuccess: ({ job }) => { setSelected(job.id); setFile(null); setName(""); if (input.current) input.current.value = ""; refresh(); } });
  const cancel = useMutation({ mutationFn: (id: string) => api.cancelLeadImport(id), onSuccess: refresh });
  const pause = useMutation({ mutationFn: (id: string) => api.pauseLeadImport(id), onSuccess: refresh });
  const resume = useMutation({ mutationFn: (id: string) => api.resumeLeadImport(id), onSuccess: refresh });
  const rename = useMutation({ mutationFn: ({ id, title }: { id: string; title: string }) => api.renameLeadImport(id, title), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => api.deleteLeadImport(id), onSuccess: () => { setSelected(null); refresh(); } });
  function choose(next: File | null) {
    setFileError(""); setFile(next);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!aiEnabled) { setFileError("AI research is temporarily unavailable. Please wait a while and try again."); return; }
    if (file && !upload.isPending) upload.mutate();
  }
  function renameJob(job: LeadImportJob) { const title = window.prompt("Research name", job.name); if (title?.trim()) rename.mutate({ id: job.id, title: title.trim() }); }
  function deleteJob(job: LeadImportJob) { if (window.confirm(`Remove research "${job.name}"?`)) remove.mutate(job.id); }
  return <div className="space-y-5">
    <form onSubmit={submit} className="ui-panel rounded-xl border border-white/10 p-5 space-y-4">
      <div><h2 className="text-base font-semibold text-white">Build your file research</h2><p className="mt-1 text-xs text-slate-400">Upload your email list. AI checks each address and saves relevant company leads in your account.</p></div>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block text-sm text-slate-300">Email file
          <input ref={input} type="file" accept=".csv,.xlsx,.xls,.pdf,.docx,.ods,.txt" onChange={(e) => choose(e.target.files?.[0] ?? null)} className="mt-2 block w-full rounded-lg border border-white/10 bg-white/[0.04] p-3 text-sm" />
          <span className="mt-1 block text-xs text-slate-500">CSV, Excel, PDF, Word or text · no fixed file-size cap</span>
        </label>
        <label className="block text-sm text-slate-300">Research name (optional)
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. October contractors" className="mt-2 w-full rounded-lg border border-white/10 bg-white/[0.04] px-3 py-2.5 text-sm text-white" />
        </label>
      </div>
      <p className="text-xs text-slate-400">Daily relevant lead limit · UTC: {quota.data?.limit == null ? "Unlimited" : `${quota.data.remaining} remaining of ${quota.data.limit}`}</p>
      {fileError && <p className="text-sm text-rose-300">{fileError}</p>}
      {upload.isError && <p className="text-sm text-rose-300">{errorText(upload.error)}</p>}
      <button type="submit" disabled={!file || upload.isPending} className="min-h-10 rounded-lg bg-indigo-600 px-5 text-sm font-semibold text-white disabled:opacity-50">{upload.isPending ? "Uploading…" : "Execute Research"}</button>
      <p className="text-xs text-slate-500">Research continues if you leave this screen.</p>
    </form>
    <div className="ui-panel rounded-xl border border-white/10 p-5">
      <h2 className="text-base font-semibold text-white">File research history</h2>
      {jobs.isError && <p className="mt-3 text-sm text-rose-300">Could not load research history.</p>}
      {!jobs.isLoading && !jobs.data?.length && <p className="mt-3 text-sm text-slate-400">No file research yet.</p>}
      <div className="mt-3 flex flex-wrap gap-2">{jobs.data?.map((job) => <button key={job.id} onClick={() => setSelected(job.id)} className={`min-h-10 rounded-lg border px-3 text-left text-xs ${selected === job.id ? "border-indigo-400 text-indigo-200" : "border-white/10 text-slate-300"}`}>{job.name} · {job.status}</button>)}</div>
      {current && <div className="mt-5 rounded-lg border border-white/10 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="font-semibold text-white">{current.name}</h3><p className="text-xs text-slate-400">{current.filename} · {current.status}</p></div><div className="flex flex-wrap gap-2">
          <button onClick={() => navigate(`/leads?import_id=${encodeURIComponent(current.id)}`)} className="rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white">View leads →</button>
          <button onClick={() => renameJob(current)} className="rounded-lg border border-white/10 px-3 py-2 text-xs text-slate-300">Rename</button>
          {activeStatus(current.status) && <button onClick={() => pause.mutate(current.id)} disabled={pause.isPending} className="min-h-11 rounded-lg border border-amber-500/30 px-3 py-2 text-xs font-semibold text-amber-300 disabled:opacity-50">{pause.isPending ? "Pausing…" : "Pause"}</button>}
          {(current.status === "paused" || current.status === "cancelled") && <button onClick={() => resume.mutate(current.id)} disabled={resume.isPending || !aiEnabled} className="min-h-11 rounded-lg bg-emerald-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">{resume.isPending ? "Resuming…" : "Resume"}</button>}
          {(activeStatus(current.status) || current.status === "paused") && <button onClick={() => cancel.mutate(current.id)} disabled={cancel.isPending} className="min-h-11 rounded-lg border border-amber-500/30 px-3 py-2 text-xs text-amber-300 disabled:opacity-50">Cancel</button>}
          {(current.status === "completed" || current.status === "cancelled") && <button onClick={() => deleteJob(current)} disabled={remove.isPending} className="min-h-11 rounded-lg border border-rose-500/30 px-3 py-2 text-xs text-rose-300 disabled:opacity-50">Remove</button>}
        </div></div>
        {(pause.isError || resume.isError || cancel.isError) && <p role="alert" className="mt-3 text-sm text-rose-300">{errorText(pause.error || resume.error || cancel.error)}</p>}
        {current.status === "paused" && <p role="status" className="mt-3 text-xs text-amber-300">Paused. An address already being researched may finish; the remaining addresses will wait.</p>}
        {current.status === "cancelled" && <p role="status" className="mt-3 text-xs text-slate-300">Cancelled with {current.pending} address{current.pending === 1 ? "" : "es"} remaining. Resume continues from here.</p>}
        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-5">{([ ["Total", current.total], ["Relevant", current.relevant], ["Irrelevant", current.irrelevant], ["Pending", current.pending], ["Failed", current.failed] ] as const).map(([label, value]) => <div key={label} className="rounded-lg bg-white/[0.04] p-3"><p className="text-xs text-slate-400">{label}</p><p className="text-lg font-semibold text-white">{value}</p></div>)}</div>
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-white/10"><div className="h-full bg-indigo-500" style={{ width: `${current.total ? Math.round((current.total - current.pending) / current.total * 100) : 0}%` }} /></div>
        <h4 className="mt-5 text-sm font-semibold text-white">Research activity</h4>
        <div className="mt-2 max-h-64 overflow-auto">{activity.data?.map((row, i) => <div key={`${row.email}-${i}`} className="flex flex-wrap justify-between gap-1 border-b border-white/5 py-2 text-xs"><span className="break-all text-slate-200">{row.email}</span><span className="text-slate-400">{row.status}{row.reason ? ` · ${row.reason}` : ""}</span></div>)}{!activity.data?.length && <p className="text-xs text-slate-500">Activity appears as research progresses.</p>}</div>
      </div>}
    </div>
  </div>;
}
