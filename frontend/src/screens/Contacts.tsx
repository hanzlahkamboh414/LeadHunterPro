import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Mail, Globe, Phone, Trash2 } from "lucide-react";
import { api } from "../api/client";
import { Spinner } from "../components/StatusChip";

/**
 * Contacts — the LEAN outreach list. Only identity + contact channels render:
 * name, email, phone (future-ready, empty today), LinkedIn. No scores, no
 * recommendation badges, no reason — that depth belongs on the Companies
 * screen. The user can delete a contact (data management), which removes its
 * dossier + discovery-cache entry so it is never re-researched.
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

  const [busyEmail, setBusyEmail] = useState<string | null>(null);

  const del = useMutation({
    mutationFn: (email: string) => api.deleteLead(email),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["leads"] }),
  });

  async function handleDelete(email: string) {
    if (!window.confirm(`Delete ${email}?\n\nYe contact remove ho jayega (dossier + discovery cache dono se).`)) {
      return;
    }
    setBusyEmail(email);
    try {
      await del.mutateAsync(email);
    } finally {
      setBusyEmail(null);
    }
  }

  return (
    <div className="px-8 py-7 max-w-6xl">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-[26px] font-semibold text-white">Contacts</h1>
          <p className="text-slate-500 text-[13.5px] mt-1">
            Sirf outreach fields — name, email, phone, LinkedIn. Baqi detail Companies me dekhein.
          </p>
        </div>
      </div>

      {isError && (
        <p className="text-[13px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2 mt-4">
          Failed to load contacts: {(error as Error).message}
        </p>
      )}

      {isLoading ? (
        <div className="flex items-center gap-2 text-slate-500 text-sm py-16 justify-center">
          <Spinner /> Loading contacts…
        </div>
      ) : !data || data.length === 0 ? (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center text-slate-500 mt-4">
          No contacts yet. Run a search on the <span className="text-indigo-400">Research</span> screen.
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
              {data.map((c) => (
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
    </div>
  );
}