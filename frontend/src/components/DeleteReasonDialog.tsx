/**
 * DeleteReasonDialog — the MANDATORY "why are you deleting?" step.
 *
 * A delete without a reason teaches the AI nothing (and a bare confirm box
 * taught it lies: every click looked like "not our client"). This dialog makes
 * the reason STRUCTURED — the user picks one of six buttons, never free text —
 * and the delete button stays disabled until one is chosen.
 *
 * Only ``not_our_client`` feeds identity learning (fit_learning company/domain
 * rejection); the backend applies the corroboration gaming-guard there. The
 * other five reasons are hygiene — they delete the lead but teach nothing, so
 * a cleaning spree can never burn a whole market's data.
 */

import { useState } from "react";
import { Trash2, X } from "lucide-react";
import { Spinner } from "./StatusChip";

export type DeleteReason =
  | "not_our_client"
  | "bad_data"
  | "duplicate"
  | "already_contacted"
  | "low_quality"
  | "other";

export const DELETE_REASONS: { slug: DeleteReason; label: string; hint: string }[] = [
  {
    slug: "not_our_client",
    label: "Hamara client nahi",
    hint: "Ye company kabhi hamara customer nahi ban sakti — AI isay yaad rakhega",
  },
  {
    slug: "bad_data",
    label: "Email / company ka data galat hai",
    hint: "Email bounce, company exist nahi karti, ya details ghalat hain",
  },
  {
    slug: "duplicate",
    label: "Duplicate lead hai",
    hint: "Yehi lead pehle se list me maujood hai",
  },
  {
    slug: "already_contacted",
    label: "Pehle se contact ho chuka hai",
    hint: "Is lead ko already approach kar chuke hain",
  },
  {
    slug: "low_quality",
    label: "Lead weak hai",
    hint: "Data theek hai magar lead qabil-e-tawajjo nahi",
  },
  {
    slug: "other",
    label: "Koi aur wajah",
    hint: "Upar se koi wajah match nahi hoti",
  },
];

export default function DeleteReasonDialog({
  count,
  busy,
  onClose,
  onConfirm,
}: {
  /** How many leads this delete covers (1 = single, N = bulk). */
  count: number;
  /** While the delete request(s) are in flight — disables everything. */
  busy: boolean;
  onClose: () => void;
  /** Called with the chosen reason — only after a MANDATORY choice. */
  onConfirm: (reason: DeleteReason) => void;
}) {
  const [reason, setReason] = useState<DeleteReason | null>(null);

  if (count === 0) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={busy ? undefined : onClose}
    >
      <div
        className="w-full max-w-md rounded-2xl border border-white/10 bg-[#11151E] p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between">
          <div>
            <h2 className="text-[15px] font-semibold text-white">
              Delete {count > 1 ? `${count} leads` : "lead"}
            </h2>
            <p className="mt-1 text-[12.5px] text-slate-400">
              Delete karne ki wajah batayein — ye AI ki learning ke liye zaroori hai.
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded p-1 text-slate-500 hover:text-slate-200 disabled:opacity-50"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="mt-4 flex flex-col gap-1.5">
          {DELETE_REASONS.map((r) => (
            <button
              key={r.slug}
              onClick={() => setReason(r.slug)}
              disabled={busy}
              className={`rounded-lg border px-3 py-2.5 text-left transition-colors ${
                reason === r.slug
                  ? "border-indigo-500/60 bg-indigo-500/15"
                  : "border-white/10 bg-white/[0.03] hover:bg-white/[0.06]"
              } disabled:opacity-50`}
            >
              <span className="flex items-center gap-2 text-[13px] font-medium text-slate-100">
                <span
                  className={`h-3.5 w-3.5 shrink-0 rounded-full border ${
                    reason === r.slug
                      ? "border-indigo-400 bg-indigo-500"
                      : "border-slate-500"
                  }`}
                />
                {r.label}
              </span>
              <span className="mt-0.5 block pl-[22px] text-[11.5px] text-slate-500">
                {r.hint}
              </span>
            </button>
          ))}
        </div>

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="rounded-lg px-3.5 py-2 text-[13px] font-medium text-slate-300 hover:bg-white/[0.06] disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={() => reason && onConfirm(reason)}
            // Mandatory choice: no reason -> no delete (the dialog's whole point).
            disabled={busy || reason === null}
            className="inline-flex items-center gap-1.5 rounded-lg bg-rose-600 px-3.5 py-2 text-[13px] font-semibold text-white hover:bg-rose-500 disabled:opacity-40"
          >
            {busy ? <Spinner className="h-3.5 w-3.5" /> : <Trash2 className="w-3.5 h-3.5" />}
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}
