import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  Download,
  Save,
  Search,
  StickyNote,
  Trash2,
  Voicemail,
} from "lucide-react";
import { api } from "../api/client";
import { CopyButton } from "../components/CopyButton";
import { PageHeader } from "../components/PageHeader";
import { Select } from "../components/Select";
import { useAuth } from "../contexts/AuthContext";
import { US_STATE_CODES, US_STATES } from "../data/locations";
import type { PhoneLead, PhoneSaved, PhoneSearchResult } from "../types";

/** +15039573452 -> (503) 957-3452 for display; raw E.164 for copy/tel links. */
function prettyPhone(e164: string): string {
  if (/^\+1(\d{10})$/.test(e164)) {
    const d = e164.slice(2);
    return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  }
  return e164 || "—";
}

type Tab = "sheet" | "leads" | "contacts";

/** A tiny inline note editor — one open at a time (a lead id or saved id). */
function NoteEditor({
  initial,
  onSave,
  onCancel,
}: {
  initial: string;
  onSave: (note: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState(initial);
  return (
    <div className="flex items-center gap-2">
      <input
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSave(text);
          if (e.key === "Escape") onCancel();
        }}
        placeholder="Call note…"
        className="flex-1 min-w-[140px] bg-white/[0.04] border border-white/10 rounded-lg px-2.5 py-1.5 text-[12.5px] text-slate-200 focus:outline-none focus:border-indigo-500/50"
      />
      <button
        onClick={() => onSave(text)}
        className="px-2 py-1.5 rounded-lg bg-indigo-500 hover:bg-indigo-400 text-white text-[12px] font-medium"
      >
        Save
      </button>
      <button
        onClick={onCancel}
        className="px-2 py-1.5 rounded-lg border border-white/10 text-slate-400 hover:text-white text-[12px]"
      >
        ✕
      </button>
    </div>
  );
}

/**
 * Phones screen (P3, calling workflow P7.5) — the Phones vertical's UI.
 *
 * A phone user searches TRADE-LESS: state + quantity is the whole form —
 * calling is about volume, so the sheet serves the pool's mixed trades and
 * every row carries its own trade. The serve is instant and exclusive (a
 * number on your sheet is on nobody else's).
 *
 * The calling workflow: work the sheet top to bottom — call the number,
 * then press ✓Lead (project doonga — saved to My Leads, number retired for
 * good), ☎Voicemail (parked 14/30/60 days, then recirculates; a 4th
 * voicemail retires it), 💾Store (keep as a contact), or 📝Note. The sheet
 * refreshes live, so the background email enricher fills the Email column
 * while you call.
 */
export default function Phones() {
  const { user } = useAuth();
  const isAdmin = Boolean(user?.is_admin);

  const [stateName, setStateName] = useState(""); // full name; "" = any state
  const [target, setTarget] = useState(25);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");
  const [result, setResult] = useState<PhoneSearchResult | null>(null);

  const [tab, setTab] = useState<Tab>("sheet");
  const [mine, setMine] = useState<PhoneLead[]>([]); // the call sheet
  const [saved, setSaved] = useState<PhoneSaved[]>([]); // leads + contacts
  const [noteLeadId, setNoteLeadId] = useState<number | null>(null);
  const [noteSavedId, setNoteSavedId] = useState<number | null>(null);

  const stateCode = US_STATE_CODES[stateName] || "";

  const refreshMine = useCallback(() => {
    api
      .phoneLeads()
      .then(setMine)
      .catch(() => undefined); // the search below surfaces real errors
  }, []);

  const refreshSaved = useCallback(() => {
    api
      .phoneSaved()
      .then(setSaved)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    refreshMine();
    refreshSaved();
  }, [refreshMine, refreshSaved]);

  // The background enricher stamps emails on claimed leads continuously —
  // poll so the Email column fills in live while the screen is open.
  useEffect(() => {
    const t = setInterval(refreshMine, 15000);
    return () => clearInterval(t);
  }, [refreshMine]);

  const runSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!stateName) {
      setError("Pick a state first.");
      return;
    }
    setError("");
    setFlash("");
    setLoading(true);
    try {
      const res = await api.phoneSearch({ state: stateCode, target });
      setResult(res);
      setTab("sheet");
      refreshMine();
    } catch (err: any) {
      setError(err?.message || "Phone search failed");
    } finally {
      setLoading(false);
    }
  };

  // -- calling-workflow actions ------------------------------------------------

  const say = (msg: string) => {
    setFlash(msg);
    // The flash is transient feedback; actions already refreshed the lists.
    setTimeout(() => setFlash((cur) => (cur === msg ? "" : cur)), 6000);
  };

  const markLead = async (id: number) => {
    try {
      await api.phoneMarkLead(id);
      say("✓ Lead saved — this number is yours and never served to anyone else.");
      setTab("leads");
    } catch (err: any) {
      setError(err?.message || "Could not mark the lead");
    } finally {
      refreshMine();
      refreshSaved();
    }
  };

  const markVoicemail = async (id: number) => {
    try {
      const r = await api.phoneMarkVoicemail(id);
      say(
        r.retired
          ? "4th voicemail — number retired from the pool for good."
          : `Voicemail ${r.voicemail_count}/4 — parked ${r.cooldown_days} days, then it recirculates.`
      );
    } catch (err: any) {
      setError(err?.message || "Could not record the voicemail");
    } finally {
      refreshMine();
    }
  };

  const storeContact = async (id: number) => {
    try {
      await api.phoneStoreContact(id);
      say("💾 Contact stored in your account.");
      setTab("contacts");
    } catch (err: any) {
      setError(err?.message || "Could not store the contact");
    } finally {
      refreshMine();
      refreshSaved();
    }
  };

  const saveLeadNote = async (leadId: number, note: string) => {
    setNoteLeadId(null);
    try {
      await api.phoneNoteLead(leadId, note);
      say("📝 Note saved (stored as a contact).");
      refreshSaved();
    } catch (err: any) {
      setError(err?.message || "Could not save the note");
    }
  };

  const saveSavedNote = async (savedId: number, note: string) => {
    setNoteSavedId(null);
    try {
      await api.phoneSetSavedNote(savedId, note);
      refreshSaved();
    } catch (err: any) {
      setError(err?.message || "Could not save the note");
    }
  };

  const deleteSaved = async (savedId: number) => {
    try {
      await api.phoneDeleteSaved(savedId);
      refreshSaved();
    } catch (err: any) {
      setError(err?.message || "Could not delete");
    }
  };

  // Live email state by lead id: a search result is a snapshot, but the
  // enrichment outcome arrives later — the polled my-leads rows are the
  // truth for any lead shown.
  const mineById = useMemo(() => {
    const m = new Map<number, PhoneLead>();
    mine.forEach((l) => m.set(l.id, l));
    return m;
  }, [mine]);

  const downloadCsv = () => {
    const rows: PhoneLead[] = mine;
    if (rows.length === 0) return;
    const header = "person,business,trade,phone,email,status,city,state,source";
    const lines = rows.map((l) =>
      [
        l.person_name, l.business_name, l.trade, l.phone, l.email,
        l.license_status, l.city, l.state, l.source,
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
    a.download = "call-sheet.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const myLeads = saved.filter((s) => s.kind === "lead");
  const myContacts = saved.filter((s) => s.kind === "contact");

  return (
    <div className="max-w-6xl mx-auto px-6 py-8 space-y-6">
      <PageHeader
        eyebrow="Phones"
        title="Call Sheet"
        subtitle="Call contractors across your state — every number on your sheet is exclusively yours. Mark what happens on each call."
      />

      {/* Search form — trade-less by design (P7.5): state + quantity */}
      <form
        onSubmit={runSearch}
        className="bg-[#0D1017] border border-white/5 rounded-xl p-5 space-y-4"
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="block text-[12px] text-slate-400 mb-1.5">State</label>
            <Select
              value={stateName}
              onChange={setStateName}
              options={[
                { value: "", label: "Select state…" },
                ...US_STATES.map((s) => ({ value: s, label: s })),
              ]}
              placeholder="Select state…"
              ariaLabel="State"
            />
          </div>
          <div>
            <label className="block text-[12px] text-slate-400 mb-1.5">
              How many numbers
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
            {loading ? "Serving numbers…" : "Get numbers"}
          </button>
          <span className="text-[11.5px] text-slate-500">
            Serves instantly from the shared pool — mixed trades, each row
            labelled. No trade picking needed.
          </span>
        </div>
      </form>

      {/* Honest serve telemetry + transient action feedback */}
      {result && (
        <div className="bg-[#0D1017] border border-white/5 rounded-xl px-4 py-3 flex flex-wrap gap-x-6 gap-y-1.5 text-[12.5px] text-slate-400">
          <span>
            <span className="text-white font-medium">{result.leads.length}</span> number
            {result.leads.length === 1 ? "" : "s"} served
          </span>
          <span>
            <span className="text-white font-medium">{result.served_from_pool}</span> from
            pool (instant)
          </span>
          {result.coverage.length > 0 && (
            <span className="text-slate-500">sources: {result.coverage.join(", ")}</span>
          )}
          {result.reason && (
            <span className="w-full text-amber-400/90">{result.reason}</span>
          )}
        </div>
      )}
      {flash && (
        <div className="bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 text-[13px] rounded-lg px-3 py-2">
          {flash}
        </div>
      )}

      {/* Tabs: Call Sheet / My Leads / My Contacts */}
      <div className="bg-[#0D1017] border border-white/5 rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-2 border-b border-white/5">
          <div className="flex gap-1">
            {(
              [
                ["sheet", "Call Sheet", mine.length],
                ["leads", "My Leads", myLeads.length],
                ["contacts", "My Contacts", myContacts.length],
              ] as [Tab, string, number][]
            ).map(([key, label, count]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={`flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[13px] font-medium transition-colors ${
                  tab === key
                    ? "bg-white/[0.06] text-white"
                    : "text-slate-400 hover:text-slate-200"
                }`}
              >
                {label}
                <span className="text-slate-500 font-normal">({count})</span>
              </button>
            ))}
          </div>
          {tab === "sheet" && mine.length > 0 && (
            <button
              onClick={downloadCsv}
              className="flex items-center gap-1.5 text-[12px] text-slate-400 hover:text-white transition-colors"
            >
              <Download className="w-3.5 h-3.5" />
              CSV
            </button>
          )}
        </div>

        {/* -- Call Sheet tab ------------------------------------------------- */}
        {tab === "sheet" && (
          mine.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-slate-500">
              {result
                ? "No numbers could be served for this search."
                : "Pick a state and get numbers — your claimed sheet appears here."}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[12.5px]">
                <thead>
                  <tr className="text-slate-500 text-left border-b border-white/5">
                    <th className="px-4 py-2.5 font-medium">Name</th>
                    <th className="px-4 py-2.5 font-medium">Business</th>
                    <th className="px-4 py-2.5 font-medium">Trade</th>
                    <th className="px-4 py-2.5 font-medium">Phone</th>
                    <th className="px-4 py-2.5 font-medium">Email</th>
                    <th className="px-4 py-2.5 font-medium">Status</th>
                    <th className="px-4 py-2.5 font-medium">Call outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {mine.map((l) => (
                    <tr key={l.id} className="border-b border-white/[0.03] hover:bg-white/[0.02]">
                      <td className="px-4 py-2.5 text-slate-300">{l.person_name || "—"}</td>
                      <td className="px-4 py-2.5 text-slate-200">{l.business_name || "—"}</td>
                      <td className="px-4 py-2.5 text-slate-400 capitalize">{l.trade || "—"}</td>
                      <td className="px-4 py-2.5">
                        <span className="inline-flex items-center gap-1.5">
                          <a
                            href={`tel:${l.phone}`}
                            className="text-indigo-300 hover:text-indigo-200"
                          >
                            {prettyPhone(l.phone)}
                          </a>
                          <CopyButton value={l.phone} label="phone" />
                        </span>
                      </td>
                      <td className="px-4 py-2.5">
                        {/* The enrichment outcome: only an email literally seen
                            on the company's own site — never a mix-in from the
                            emails vertical, never a guess. */}
                        {(() => {
                          const live = mineById.get(l.id) ?? l;
                          if (live.email) {
                            return (
                              <span className="inline-flex items-center gap-1.5">
                                <a
                                  href={`mailto:${live.email}`}
                                  className="text-indigo-300 hover:text-indigo-200"
                                >
                                  {live.email}
                                </a>
                                <CopyButton value={live.email} label="email" />
                              </span>
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
                      <td className="px-4 py-2.5">
                        {noteLeadId === l.id ? (
                          <NoteEditor
                            initial=""
                            onSave={(note) => saveLeadNote(l.id, note)}
                            onCancel={() => setNoteLeadId(null)}
                          />
                        ) : (
                          <div className="flex items-center gap-1.5">
                            <button
                              title="Lead — person said a project is coming"
                              onClick={() => markLead(l.id)}
                              className="flex items-center gap-1 px-2 py-1 rounded-md bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 text-[11.5px] font-medium"
                            >
                              <Check className="w-3 h-3" /> Lead
                            </button>
                            <button
                              title="Voicemail — park the number (recirculates after the cooldown)"
                              onClick={() => markVoicemail(l.id)}
                              className="flex items-center gap-1 px-2 py-1 rounded-md bg-amber-500/15 text-amber-300 hover:bg-amber-500/25 text-[11.5px] font-medium"
                            >
                              <Voicemail className="w-3 h-3" />
                              {l.voicemail_count > 0 ? `${l.voicemail_count}/4` : ""}
                            </button>
                            <button
                              title="Store as a contact in your account"
                              onClick={() => storeContact(l.id)}
                              className="flex items-center gap-1 px-2 py-1 rounded-md bg-indigo-500/15 text-indigo-300 hover:bg-indigo-500/25 text-[11.5px] font-medium"
                            >
                              <Save className="w-3 h-3" /> Store
                            </button>
                            <button
                              title="Add a call note"
                              onClick={() => setNoteLeadId(l.id)}
                              className="flex items-center gap-1 px-2 py-1 rounded-md bg-white/[0.06] text-slate-300 hover:bg-white/[0.12] text-[11.5px] font-medium"
                            >
                              <StickyNote className="w-3 h-3" /> Note
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}

        {/* -- My Leads / My Contacts tabs ------------------------------------ */}
        {tab !== "sheet" && (
          (() => {
            const rows = tab === "leads" ? myLeads : myContacts;
            if (rows.length === 0) {
              return (
                <div className="px-4 py-10 text-center text-[13px] text-slate-500">
                  {tab === "leads"
                    ? "No leads yet — press ✓Lead on the call sheet when someone says a project is coming."
                    : "No stored contacts yet — press 💾Store or 📝Note on the call sheet to keep a number."}
                </div>
              );
            }
            return (
              <div className="overflow-x-auto">
                <table className="w-full text-[12.5px]">
                  <thead>
                    <tr className="text-slate-500 text-left border-b border-white/5">
                      <th className="px-4 py-2.5 font-medium">Name</th>
                      <th className="px-4 py-2.5 font-medium">Business</th>
                      <th className="px-4 py-2.5 font-medium">Trade</th>
                      <th className="px-4 py-2.5 font-medium">Phone</th>
                      <th className="px-4 py-2.5 font-medium">Email</th>
                      <th className="px-4 py-2.5 font-medium">Status</th>
                      <th className="px-4 py-2.5 font-medium">Note</th>
                      <th className="px-4 py-2.5 font-medium"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((s) => (
                      <tr key={s.id} className="border-b border-white/[0.03] hover:bg-white/[0.02]">
                        <td className="px-4 py-2.5 text-slate-300">{s.person_name || "—"}</td>
                        <td className="px-4 py-2.5 text-slate-200">{s.business_name || "—"}</td>
                        <td className="px-4 py-2.5 text-slate-400 capitalize">{s.trade || "—"}</td>
                        <td className="px-4 py-2.5">
                          <span className="inline-flex items-center gap-1.5">
                            <a
                              href={`tel:${s.phone}`}
                              className="text-indigo-300 hover:text-indigo-200"
                            >
                              {prettyPhone(s.phone)}
                            </a>
                            <CopyButton value={s.phone} label="phone" />
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          {s.email ? (
                            <span className="inline-flex items-center gap-1.5">
                              <a
                                href={`mailto:${s.email}`}
                                className="text-indigo-300 hover:text-indigo-200"
                              >
                                {s.email}
                              </a>
                              <CopyButton value={s.email} label="email" />
                            </span>
                          ) : (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                        <td className="px-4 py-2.5">
                          {s.license_status ? (
                            <span
                              className={
                                s.license_status === "ACTIVE"
                                  ? "text-emerald-400"
                                  : "text-amber-400/80"
                              }
                            >
                              {s.license_status}
                            </span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="px-4 py-2.5 max-w-[240px]">
                          {noteSavedId === s.id ? (
                            <NoteEditor
                              initial={s.note}
                              onSave={(note) => saveSavedNote(s.id, note)}
                              onCancel={() => setNoteSavedId(null)}
                            />
                          ) : (
                            <button
                              onClick={() => setNoteSavedId(s.id)}
                              className={`text-left ${
                                s.note
                                  ? "text-slate-300 hover:text-white"
                                  : "text-slate-500 italic hover:text-slate-300"
                              }`}
                            >
                              {s.note || "add note…"}
                            </button>
                          )}
                        </td>
                        <td className="px-4 py-2.5">
                          <button
                            title="Delete from your account"
                            onClick={() => deleteSaved(s.id)}
                            className="p-1.5 rounded-md text-slate-500 hover:text-red-400 hover:bg-red-500/10"
                          >
                            <Trash2 className="w-3.5 h-3.5" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })()
        )}
      </div>
    </div>
  );
}
