import { useState } from "react";
import { KeyRound, ShieldCheck, Info } from "lucide-react";
import { getStoredApiKey, setStoredApiKey } from "../api/client";
import { PageHeader } from "../components/PageHeader";

export default function Settings() {
  const [apiKey, setApiKey] = useState(getStoredApiKey());
  const [showKey, setShowKey] = useState(false);
  const [saved, setSaved] = useState(false);

  function save() {
    setStoredApiKey(apiKey.trim());
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  return (
    <div className="px-8 py-7 max-w-2xl">
      <PageHeader
        eyebrow="LeadHunter Pro"
        title="Settings"
        subtitle="API access and connection details."
      />

      {/* API key */}
      <div className="mt-6 rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <KeyRound className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">API key</h2>
            <p className="text-[12.5px] text-slate-500">
              Only needed if the backend sets <code>LEADS_API_KEY</code>. Sent as{" "}
              <code>X-API-Key</code>. Stored in this browser (localStorage).
            </p>
          </div>
        </div>
        <div className="mt-4 flex gap-2">
          <input
            type={showKey ? "text" : "password"}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="Leave blank if no key is configured"
            className="flex-1 bg-white/[0.04] border border-white/5 rounded-lg px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40"
          />
          <button
            onClick={() => setShowKey((v) => !v)}
            className="rounded-lg border border-white/5 px-3 text-[12.5px] text-slate-400 hover:bg-white/[0.04]"
          >
            {showKey ? "Hide" : "Show"}
          </button>
          <button
            onClick={save}
            className="rounded-lg bg-indigo-600 px-4 text-[13px] font-semibold text-white hover:bg-indigo-500"
          >
            {saved ? "Saved ✓" : "Save"}
          </button>
        </div>
      </div>

      {/* Connection */}
      <div className="mt-5 rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <ShieldCheck className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">System status</h2>
            <p className="text-[12.5px] text-slate-500">Backend connection is live.</p>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-1.5 text-[13px] text-emerald-400">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
          Operational
        </div>
      </div>

      <div className="mt-5 flex items-start gap-2.5 text-[12px] text-slate-500">
        <Info className="w-4 h-4 shrink-0 mt-0.5" />
        <p>
          Auth is the M12 baseline — an optional API key. Full user accounts are
          a later phase of the roadmap.
        </p>
      </div>
    </div>
  );
}
