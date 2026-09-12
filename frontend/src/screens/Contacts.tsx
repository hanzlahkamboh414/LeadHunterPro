import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe, Link2, Mail, Phone, Search, Trash2, Users } from "lucide-react";
import { api } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import { Spinner } from "../components/StatusChip";
import DeleteReasonDialog, { type DeleteReason } from "../components/DeleteReasonDialog";

/**
 * Contacts — the LEAN outreach list. Only identity + contact channels render:
 * name, email, phone (future-ready, empty today), LinkedIn. No scores, no
 * recommendation badges, no reason — that depth belongs on the Companies
 * screen. The user can delete a contact (data management), which removes its
 * dossier + discovery-cache entry so it is never re-researched — after picking
 * a MANDATORY structured reason (the delete dialog) so the AI learns honestly.
 */
export default function Contacts() {
  const qc = useQueryClient();
  const { data, isLoading, isError, error } = useQuery({
    // folder="*" = every place: Contacts is the global outreach list, so a
    // folded lead must still appear here. Prefix ["leads"] keeps mutation
    // invalidations (delete/organize) reaching this query.
    queryKey: ["leads", { scope: "all" }],
    queryFn: () => api.listLeads({ folder: "*", limit: 1000 }),
  });

  // Client-side live filter — the whole outreach list is already downloaded
  // (limit 1000), so searching name/company/email/LinkedIn needs no round trip.
  // contactFilter comes from the overview chips: LinkedIn/Phone cards toggle
  // "only contacts that have it".
  const [filter, setFilter] = useState("");
  const [contactFilter, setContactFilter] = useState<"" | "linkedin" | "phone">("");
  const q = filter.trim().toLowerCase();
  const contacts = data ?? [];
  const visible = contacts.filter((c) => {
    if (contactFilter === "linkedin" && !c.linkedin) return false;
    if (contactFilter === "phone" && !c.phone) return false;
    if (!q) return true;
    return [c.person, c.company, c.email, c.linkedin].some((v) => (v ?? "").toLowerCase().includes(q));
  });
  const withLinkedIn = contacts.filter((c) => c.linkedin).length;
  const withPhone = contacts.filter((c) => c.phone).length;

  const [busyEmail, setBusyEmail] = useState<string | null>(null);
  // The pending delete: which email the dialog will remove once a reason is
  // chosen. null = dialog closed.
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);

  const del = useMutation({
    mutationFn: (v: { email: string; reason: DeleteReason }) =>
      api.deleteLead(v.email, v.reason),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["leads"] }),
  });

  function handleDelete(email: string) {
    setPendingDelete(email); // open the mandatory-reason dialog
  }

  async function confirmDelete(reason: DeleteReason) {
    if (!pendingDelete) return;
    setBusyEmail(pendingDelete);
    try {
      await del.mutateAsync({ email: pendingDelete, reason });
      setPendingDelete(null);
    } finally {
      setBusyEmail(null);
    }
  }

  return (
    <div className="px-8 py-7 max-w-6xl">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <PageHeader
          eyebrow="LeadHunter Pro"
          title="Contacts"
          subtitle="Sirf outreach fields — name, email, phone, LinkedIn. Baqi detail Companies me dekhein."
        />
      </div>

      {/* Overview chips + live search — the outreach list at a glance.
          Every chip ACTS: LinkedIn/Phone narrow the list, Total resets. */}
      {contacts.length > 0 && (
        <div className="mt-5 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => {
              setContactFilter("");
              setFilter("");
            }}
            className={`flex items-center gap-2 rounded-xl border px-4 py-2.5 text-left transition-colors hover:bg-white/[0.04] ${
              contactFilter || filter ? "border-indigo-500/50 bg-indigo-500/[0.08]" : "border-white/5 bg-white/[0.02]"
            }`}
          >
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-indigo-500/15">
              <Users className="h-[15px] w-[15px] text-indigo-400" strokeWidth={1.9} />
            </div>
            <div>
              <div className="text-[16px] font-semibold text-white leading-tight">
                {contacts.length.toLocaleString()}
              </div>
              <div className="text-[11px] text-slate-500">
                {contactFilter || filter ? "all contacts (reset)" : "contacts"}
              </div>
            </div>
          </button>
          <button
            type="button"
            onClick={() => setContactFilter(contactFilter === "linkedin" ? "" : "linkedin")}
            className={`flex items-center gap-2 rounded-xl border px-4 py-2.5 text-left transition-colors hover:bg-white/[0.04] ${
              contactFilter === "linkedin" ? "border-indigo-500/50 bg-indigo-500/[0.08]" : "border-white/5 bg-white/[0.02]"
            }`}
          >
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-sky-500/15">
              <Link2 className="h-[15px] w-[15px] text-sky-300" strokeWidth={1.9} />
            </div>
            <div>
              <div className="text-[16px] font-semibold text-white leading-tight">
                {withLinkedIn.toLocaleString()}
                <span className="ml-1 text-[11.5px] font-normal text-slate-500">
                  ({contacts.length ? Math.round((withLinkedIn / contacts.length) * 100) : 0}%)
                </span>
              </div>
              <div className="text-[11px] text-slate-500">
                {contactFilter === "linkedin" ? "showing only these" : "with LinkedIn"}
              </div>
            </div>
          </button>
          {withPhone > 0 && (
            <button
              type="button"
              onClick={() => setContactFilter(contactFilter === "phone" ? "" : "phone")}
              className={`flex items-center gap-2 rounded-xl border px-4 py-2.5 text-left transition-colors hover:bg-white/[0.04] ${
                contactFilter === "phone" ? "border-indigo-500/50 bg-indigo-500/[0.08]" : "border-white/5 bg-white/[0.02]"
              }`}
            >
              <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-emerald-500/15">
                <Phone className="h-[15px] w-[15px] text-emerald-300" strokeWidth={1.9} />
              </div>
              <div>
                <div className="text-[16px] font-semibold text-white leading-tight">
                  {withPhone.toLocaleString()}
                </div>
                <div className="text-[11px] text-slate-500">
                  {contactFilter === "phone" ? "showing only these" : "with phone"}
                </div>
              </div>
            </button>
          )}
          <div className="relative ml-auto w-full sm:w-64">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Search name, company, email…"
              className="w-full rounded-lg border border-white/5 bg-white/[0.04] py-2.5 pl-9 pr-3 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:border-indigo-500/40 focus:ring-2 focus:ring-indigo-500/40"
            />
          </div>
        </div>
      )}

      {isError && (
        <p className="text-[13px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2 mt-4">
          Failed to load contacts: {(error as Error).message}
        </p>
      )}

      {isLoading ? (
        <div className="flex items-center gap-2 text-slate-500 text-sm py-16 justify-center">
          <Spinner /> Loading contacts…
        </div>
      ) : contacts.length === 0 ? (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center text-slate-500 mt-4">
          No contacts yet. Run a search on the <span className="text-indigo-400">Research</span> screen.
        </div>
      ) : visible.length === 0 ? (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center text-slate-500 mt-4">
          No contacts match{" "}
          {contactFilter === "linkedin" ? "“with LinkedIn”" : contactFilter === "phone" ? "“with phone”" : ""}
          {filter.trim() ? `${contactFilter ? " + " : ""}“${filter.trim()}”` : ""} —{" "}
          <button
            onClick={() => {
              setContactFilter("");
              setFilter("");
            }}
            className="text-indigo-400 hover:underline"
          >
            show all
          </button>
          .
        </div>
      ) : (
        <div className="mt-4 overflow-x-auto rounded-xl border border-white/5">
          <table className="w-full text-[13.5px]">
            <thead>
              <tr className="bg-white/[0.02] text-left text-[12px] uppercase tracking-wide text-slate-500">
                <th className="px-4 py-3 font-medium">Person</th>
                <th className="px-4 py-3 font-medium">Email</th>
                <th className="px-4 py-3 font-medium">Phone</th>
                <th className="px-4 py-3 font-medium">LinkedIn</th>
                <th className="px-4 py-3 font-medium w-12" />
              </tr>
            </thead>
            <tbody>
              {visible.map((c) => (
                <tr key={c.email} className="border-t border-white/5 hover:bg-white/[0.03]">
                  <td className="px-4 py-3">
                    <div className="font-medium text-white">{c.person || "—"}</div>
                    <div className="text-[12px] text-slate-500">{c.company}</div>
                  </td>
                  <td className="px-4 py-3">
                    <a
                      href={`mailto:${c.email}`}
                      className="inline-flex items-center gap-1.5 text-indigo-400 hover:underline break-all"
                    >
                      <Mail className="w-3.5 h-3.5 shrink-0" />
                      {c.email}
                    </a>
                  </td>
                  <td className="px-4 py-3 text-slate-300">
                    {c.phone ? (
                      <span className="inline-flex items-center gap-1.5">
                        <Phone className="w-3.5 h-3.5 shrink-0 text-slate-500" />
                        {c.phone}
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {c.linkedin ? (
                      <a
                        href={/^https?:\/\//i.test(c.linkedin) ? c.linkedin : `https://${c.linkedin}`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1.5 text-indigo-400 hover:underline break-all"
                      >
                        <Globe className="w-3.5 h-3.5 shrink-0" />
                        {c.linkedin}
                      </a>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => handleDelete(c.email)}
                      disabled={busyEmail === c.email}
                      aria-label={`Delete ${c.email}`}
                      title="Delete (data management)"
                      className="inline-flex items-center justify-center rounded-lg border border-white/5 p-1.5 text-slate-500 hover:text-rose-300 hover:border-rose-400/30 disabled:opacity-50"
                    >
                      {busyEmail === c.email ? (
                        <Spinner className="h-4 w-4" />
                      ) : (
                        <Trash2 className="w-4 h-4" />
                      )}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Mandatory structured reason before any delete — the AI learning gate. */}
      <DeleteReasonDialog
        count={pendingDelete ? 1 : 0}
        busy={del.isPending}
        onClose={() => setPendingDelete(null)}
        onConfirm={confirmDelete}
      />
    </div>
  );
}