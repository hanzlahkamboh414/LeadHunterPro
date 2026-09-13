import { useCallback, useEffect, useMemo, useState } from "react";
import { Phone as PhoneIcon, Search, Download } from "lucide-react";
import { api } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import { Select } from "../components/Select";
import { useAuth } from "../contexts/AuthContext";
import { CITIES_BY_STATE, TRADES, US_STATE_CODES, US_STATES } from "../data/locations";
import type { PhoneLead, PhoneSearchResult } from "../types";

/** +15039573452 -> (503) 957-3452 for display; raw E.164 for copy/tel links. */
function prettyPhone(e164: string): string {
  if (/^\+1(\d{10})$/.test(e164)) {
    const d = e164.slice(2);
    return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  }
  return e164 || "—";
}

/**
 * Phones screen (P3) — the Phones vertical's UI.
 *
 * A search is instant-first: the shared pool serves whatever it holds, the
 * backend live-fetches only the gap from license boards. The telemetry line
 * reports honestly what came from where — and the shortfall reason when a
 * trade/state has no covering source yet (never a fake result).
 */
export default function Phones() {
  const { user } = useAuth();
  const isAdmin = Boolean(user?.is_admin);

  const [trade, setTrade] = useState("");
  const [stateName, setStateName] = useState(""); // full name; "" = any state
  const [city, setCity] = useState("");
  const [target, setTarget] = useState(25);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<PhoneSearchResult | null>(null);
  const [mine, setMine] = useState<PhoneLead[]>([]);

  const stateCode = US_STATE_CODES[stateName] || "";

  // City options follow the chosen state; "City, ST" strings → bare city.
  const cityOptions = useMemo(() => {
    const list = stateName ? CITIES_BY_STATE[stateName] || [] : [];
    return [
      { value: "", label: "Any city" },
      ...list.map((c) => ({ value: c.split(",")[0].trim(), label: c.split(",")[0].trim() })),
    ];
  }, [stateName]);

  const refreshMine = useCallback(() => {
    api
      .phoneLeads()
      .then(setMine)
      .catch(() => undefined); // the search below surfaces real errors
  }, []);

  useEffect(refreshMine, [refreshMine]);

  // The background enricher stamps emails on claimed leads continuously —
  // poll so the Email column fills in live while the screen is open.
  useEffect(() => {
    const t = setInterval(refreshMine, 15000);
    return () => clearInterval(t);
  }, [refreshMine]);

  // Live email state by lead id: a search result is a snapshot, but the
  // enrichment outcome arrives later — the polled my-leads rows are the
  // truth for any lead shown.
  const mineById = useMemo(() => {
    const m = new Map<number, PhoneLead>();
    mine.forEach((l) => m.set(l.id, l));
    return m;
  }, [mine]);

  const runSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!trade) {
      setError("Pick a trade first.");
      return;
    }
    setError("");
    setLoading(true);
    try {
      const res = await api.phoneSearch({
        trade,
        state: stateCode,
        city,
        target,
      });
      setResult(res);
      refreshMine();
    } catch (err: any) {
      setError(err?.message || "Phone search failed");
    } finally {
      setLoading(false);
    }
  };

  const downloadCsv = () => {
    const rows = result?.leads.length ? result.leads : mine;
    if (rows.length === 0) return;
    const header =
      "person,business,phone,email,trade,city,state,source,license_status";
    const lines = rows.map((l) =>
      [
        l.person_name, l.business_name, l.phone, l.email, l.trade,
        l.city, l.state, l.source, l.license_status,
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
    a.download = "phone-leads.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const shown = result?.leads.length ? result.leads : mine;
  const showMine = !result || result.leads.length === 0;

  return (
    <div className="max-w-6xl mx-auto px-6 py-8 space-y-6">
      <PageHeader
        eyebrow="Phones"
        title="Phone Leads"
        subtitle="Direct phone numbers of contractors, harvested from state license boards — every lead is exclusively yours."
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
              max={isAdmin ? 5000 : 1000}
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
            {loading ? "Serving leads…" : "Get phone leads"}
          </button>
          <span className="text-[11.5px] text-slate-500">
            Serves instantly from the shared pool; live license-board fetch fills any gap.
          </span>
        </div>
      </form>

      {/* Honest harvest telemetry — what came from where */}
      {result && (
        <div className="bg-[#0D1017] border border-white/5 rounded-xl px-4 py-3 flex flex-wrap gap-x-6 gap-y-1.5 text-[12.5px] text-slate-400">
          <span>
            <span className="text-white font-medium">{result.leads.length}</span> lead
            {result.leads.length === 1 ? "" : "s"} served
          </span>
          <span>
            <span className="text-white font-medium">{result.served_from_pool}</span> from
            pool (instant)
          </span>
          <span>
            <span className="text-white font-medium">{result.fetched_live}</span> fetched
            live
          </span>
          <span>
            <span className="text-white font-medium">{result.stocked_new}</span> added to
            pool
          </span>
          {result.banked_other_trade > 0 && (
            <span>
              {result.banked_other_trade} banked for other trades
            </span>
          )}
          {result.coverage.length > 0 && (
            <span className="text-slate-500">sources: {result.coverage.join(", ")}</span>
          )}
          {result.reason && (
            <span className="w-full text-amber-400/90">{result.reason}</span>
          )}
        </div>
      )}

      {/* Results / my leads */}
      <div className="bg-[#0D1017] border border-white/5 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
          <div className="flex items-center gap-2 text-[13.5px] font-medium text-white">
            <PhoneIcon className="w-4 h-4 text-indigo-400" />
            {showMine ? "My phone leads" : "This search"}
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
              ? "No leads could be served for this search."
              : "Search for a trade to get phone leads — your claimed leads also appear here."}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="text-slate-500 text-left border-b border-white/5">
                  <th className="px-4 py-2.5 font-medium">Person</th>
                  <th className="px-4 py-2.5 font-medium">Business</th>
                  <th className="px-4 py-2.5 font-medium">Phone</th>
                  <th className="px-4 py-2.5 font-medium">Email</th>
                  <th className="px-4 py-2.5 font-medium">Trade</th>
                  <th className="px-4 py-2.5 font-medium">Location</th>
                  <th className="px-4 py-2.5 font-medium">Source</th>
                  <th className="px-4 py-2.5 font-medium">License</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((l) => (
                  <tr key={l.id} className="border-b border-white/[0.03] hover:bg-white/[0.02]">
                    <td className="px-4 py-2.5 text-slate-300">{l.person_name || "—"}</td>
                    <td className="px-4 py-2.5 text-slate-200">{l.business_name || "—"}</td>
                    <td className="px-4 py-2.5">
                      <a
                        href={`tel:${l.phone}`}
                        className="text-indigo-300 hover:text-indigo-200"
                      >
                        {prettyPhone(l.phone)}
                      </a>
                    </td>
                    <td className="px-4 py-2.5">
                      {/* The enrichment outcome: only an email literally seen
                          on the company's own site — never a mix-in from the
                          emails vertical, never a guess. */}
                      {(() => {
                        const live = mineById.get(l.id) ?? l;
                        if (live.email) {
                          return (
                            <a
                              href={`mailto:${live.email}`}
                              className="text-indigo-300 hover:text-indigo-200"
                            >
                              {live.email}
                            </a>
                          );
                        }
                        if (live.email_status === "pending") {
                          return (
                            <span className="text-slate-500 italic">
                              finding…
                            </span>
                          );
                        }
                        return <span className="text-slate-600">—</span>;
                      })()}
                    </td>
                    <td className="px-4 py-2.5 text-slate-400 capitalize">{l.trade || "—"}</td>
                    <td className="px-4 py-2.5 text-slate-400">
                      {[l.city, l.state].filter(Boolean).join(", ") || "—"}
                    </td>
                    <td className="px-4 py-2.5 text-slate-500">{l.source || "—"}</td>
                    <td className="px-4 py-2.5">
                      {l.license_status ? (
                        <span
                          className={
                            l.license_status === "ACTIVE"
                              ? "text-emerald-400"
                              : "text-amber-400/80"
                          }
                        >
                          {l.license_status}
                        </span>
                      ) : (
                        "—"
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
