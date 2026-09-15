import { useCallback, useEffect, useState } from "react";
import { Briefcase as BriefcaseIcon, Search, Download } from "lucide-react";
import { api } from "../api/client";
import { CopyButton } from "../components/CopyButton";
import { PageHeader } from "../components/PageHeader";
import { Select } from "../components/Select";
import { CITIES_BY_STATE, TRADES, US_STATE_CODES, US_STATES } from "../data/locations";
import type { LinkedInLead, LinkedInSearchResult } from "../types";

/**
 * LinkedIn screen (P4) — the LinkedIn vertical's UI.
 *
 * Pool-only by design: the inventory is a BYPRODUCT of email research (every
 * researched dossier whose decision-maker carries a linkedin.com/in/ URL
 * stocks a row). There is no live LinkedIn fetch — so a shortfall is reported
 * honestly and the pool grows as research runs. Every served lead is
 * exclusively the caller's.
 */
export default function LinkedIn() {
  const [trade, setTrade] = useState("");
  const [stateName, setStateName] = useState(""); // full name; "" = any state
  const [city, setCity] = useState("");
  const [target, setTarget] = useState(25);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<LinkedInSearchResult | null>(null);
  const [mine, setMine] = useState<LinkedInLead[]>([]);
  const [poolTotal, setPoolTotal] = useState<number | null>(null);

  const stateCode = US_STATE_CODES[stateName] || "";

  // City options follow the chosen state; "City, ST" strings → bare city.
  const cityOptions = (() => {
    const list = stateName ? CITIES_BY_STATE[stateName] || [] : [];
    return [
      { value: "", label: "Any city" },
      ...list.map((c) => ({ value: c.split(",")[0].trim(), label: c.split(",")[0].trim() })),
    ];
  })();

  const refreshMine = useCallback(() => {
    api
      .linkedinLeads()
      .then(setMine)
      .catch(() => undefined); // the search below surfaces real errors
    api
      .linkedinStats()
      .then((s) => setPoolTotal(s.total))
      .catch(() => undefined);
  }, []);

  useEffect(refreshMine, [refreshMine]);

  const runSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!trade) {
      setError("Pick a trade first.");
      return;
    }
    setError("");
    setLoading(true);
    try {
      const res = await api.linkedinSearch({
        trade,
        state: stateCode,
        city,
        target,
      });
      setResult(res);
      refreshMine();
    } catch (err: any) {
      setError(err?.message || "LinkedIn search failed");
    } finally {
      setLoading(false);
    }
  };

  const downloadCsv = () => {
    const rows = result?.leads.length ? result.leads : mine;
    if (rows.length === 0) return;
    const header =
      "person,role,linkedin_url,company,domain,trade,city,state,source_email";
    const lines = rows.map((l) =>
      [
        l.person_name, l.role, l.linkedin_url, l.company_name, l.domain,
        l.trade, l.city, l.state, l.source_email,
      ]
        .map((v) => `"${(v || "").replace(/"/g, '""')}"`)
        .join(","),
    );
    const blob = new Blob([[header, ...lines].join("\n")], {
      type: "text/csv",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "linkedin-leads.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const shown = result?.leads.length ? result.leads : mine;
  const showMine = !result || result.leads.length === 0;

  return (
    <div className="max-w-6xl mx-auto px-6 py-8 space-y-6">
      <PageHeader
        eyebrow="LinkedIn"
        title="LinkedIn Profiles"
        subtitle="Decision-maker profiles found during email research — every served lead is exclusively yours. The pool grows as research runs; there is no live LinkedIn fetch."
      />

      {/* Search form */}
      <form
        onSubmit={runSearch}
        className="bg-[#0D1017] border border-white/5 rounded-xl p-5 space-y-4"
      >
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label className="block text-[12px] text-slate-400 mb-1.5">Trade</label>
            <Select
              value={trade}
              onChange={setTrade}
              options={TRADES.map((t) => ({ value: t, label: t }))}
              placeholder="Select trade…"
              ariaLabel="Trade"
            />
          </div>
          <div>
            <label className="block text-[12px] text-slate-400 mb-1.5">State</label>
            <Select
              value={stateName}
              onChange={(v) => {
                setStateName(v);
                setCity(""); // city list follows the state
              }}
              options={[
                { value: "", label: "Any state" },
                ...US_STATES.map((s) => ({ value: s, label: s })),
              ]}
              placeholder="Any state"
              ariaLabel="State"
            />
          </div>
          <div>
            <label className="block text-[12px] text-slate-400 mb-1.5">City (optional)</label>
            <Select
              value={city}
              onChange={setCity}
              options={cityOptions}
              placeholder={stateName ? "Any city" : "Pick a state first"}
              disabled={!stateName}
              ariaLabel="City"
            />
          </div>
          <div>
            <label className="block text-[12px] text-slate-400 mb-1.5">
              How many leads
            </label>
            <input
              type="number"
              min={1}
              max={5000}
              value={target}
              onChange={(e) => setTarget(Number(e.target.value) || 1)}
              className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2.5 text-[13px] text-slate-200 focus:outline-none focus:border-indigo-500/50"
            />
          </div>
        </div>

        {error && (
          <div className="bg-red-500/10 border border-red-500/20 text-red-400 text-[13px] rounded-lg px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={loading}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-indigo-500 hover:bg-indigo-400 text-white text-[13px] font-medium transition-colors disabled:opacity-50"
          >
            <Search className="w-4 h-4" />
            {loading ? "Serving leads…" : "Get LinkedIn leads"}
          </button>
          <span className="text-[11.5px] text-slate-500">
            Serves instantly from the research byproduct pool — no quota, no live fetch.
            {poolTotal !== null && ` Pool holds ${poolTotal} profile(s) so far.`}
          </span>
        </div>
      </form>

      {/* Honest serve telemetry — pool-only, so a shortfall is stated plainly */}
      {result && (
        <div className="bg-[#0D1017] border border-white/5 rounded-xl px-4 py-3 flex flex-wrap gap-x-6 gap-y-1.5 text-[12.5px] text-slate-400">
          <span>
            <span className="text-white font-medium">{result.leads.length}</span> lead
            {result.leads.length === 1 ? "" : "s"} served
          </span>
          <span>
            <span className="text-white font-medium">{result.served_from_pool}</span> from
            research pool (instant)
          </span>
          {result.reason && (
            <span className="w-full text-amber-400/90">{result.reason}</span>
          )}
        </div>
      )}

      {/* Results / my leads */}
      <div className="bg-[#0D1017] border border-white/5 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
          <div className="flex items-center gap-2 text-[13.5px] font-medium text-white">
            <BriefcaseIcon className="w-4 h-4 text-indigo-400" />
            {showMine ? "My LinkedIn leads" : "This search"}
            <span className="text-slate-500 font-normal">({shown.length})</span>
          </div>
          {shown.length > 0 && (
            <button
              onClick={downloadCsv}
              className="flex items-center gap-1.5 text-[12px] text-slate-400 hover:text-white transition-colors"
            >
              <Download className="w-3.5 h-3.5" />
              CSV
            </button>
          )}
        </div>

        {shown.length === 0 ? (
          <div className="px-4 py-10 text-center text-[13px] text-slate-500">
            {result
              ? "No matching profiles in the pool for this search — it grows as email research completes."
              : "Search a trade to claim LinkedIn profiles — your claimed leads also appear here."}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="text-slate-500 text-left border-b border-white/5">
                  <th className="px-4 py-2.5 font-medium">Person</th>
                  <th className="px-4 py-2.5 font-medium">Role</th>
                  <th className="px-4 py-2.5 font-medium">Company</th>
                  <th className="px-4 py-2.5 font-medium">Trade</th>
                  <th className="px-4 py-2.5 font-medium">Location</th>
                  <th className="px-4 py-2.5 font-medium">LinkedIn</th>
                  <th className="px-4 py-2.5 font-medium">Found via</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((l) => (
                  <tr key={l.id} className="border-b border-white/[0.03] hover:bg-white/[0.02]">
                    <td className="px-4 py-2.5 text-slate-200">{l.person_name || "—"}</td>
                    <td className="px-4 py-2.5 text-slate-400">{l.role || "—"}</td>
                    <td className="px-4 py-2.5 text-slate-300">
                      {l.company_name || "—"}
                      {l.domain && (
                        <span className="block text-[11px] text-slate-500">{l.domain}</span>
                      )}
                    </td>
                    <td className="px-4 py-2.5 text-slate-400 capitalize">{l.trade || "—"}</td>
                    <td className="px-4 py-2.5 text-slate-400">
                      {[l.city, l.state].filter(Boolean).join(", ") || "—"}
                    </td>
                    <td className="px-4 py-2.5">
                      {l.linkedin_url ? (
                        <span className="inline-flex items-center gap-1.5">
                          <a
                            href={l.linkedin_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-indigo-300 hover:text-indigo-200"
                          >
                            View profile ↗
                          </a>
                          <CopyButton value={l.linkedin_url} label="LinkedIn URL" />
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-4 py-2.5">
                      {/* The email this profile was a byproduct OF — the
                          research breadcrumb, honest about provenance. */}
                      {l.source_email ? (
                        <span className="inline-flex items-center gap-1.5">
                          <a
                            href={`mailto:${l.source_email}`}
                            className="text-slate-500 hover:text-slate-300"
                          >
                            {l.source_email}
                          </a>
                          <CopyButton value={l.source_email} label="email" />
                        </span>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
