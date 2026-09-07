/**
 * ManageMenu — the folder/tag organization popover on the Companies screen.
 *
 * The fixed left rail is gone: BROWSING lives in the chip bar on Leads.tsx
 * (Inbox · All · 📁 folder · # tag), and this dropdown owns the CREATE / RENAME
 * / DELETE side — a "naya folder" field on top, then every folder and tag with
 * its count and inline ✏️ / 🗑 buttons. Data (the folder catalog + global tag
 * counts) is passed in from Leads.tsx so the chips and this menu always agree;
 * the mutations here invalidate the shared ["folders"] / ["leads"] / ["dates"]
 * cache on success.
 *
 * Clicking a folder row selects that folder's view and closes the menu — the
 * mailbox navigation is not trapped inside the popover.
 */

import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, Plus, Tag as TagIcon, Trash2, X } from "lucide-react";
import { api } from "../api/client";
import { Spinner } from "./StatusChip";
import type { FoldersOut } from "../types";

type LabelKind = "folder" | "tag";

export default function ManageMenu({
  open,
  onClose,
  catalog,
  tagCounts,
  activeFolder,
  onSelectFolder,
}: {
  open: boolean;
  onClose: () => void;
  catalog: FoldersOut | undefined;
  tagCounts: Map<string, number>;
  activeFolder: string;
  /** "" = Unfiled inbox, "*" = All, otherwise a folder name. */
  onSelectFolder: (value: string) => void;
}) {
  const qc = useQueryClient();
  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: ["leads"] });
    qc.invalidateQueries({ queryKey: ["folders"] });
    qc.invalidateQueries({ queryKey: ["dates"] });
  };

  const folders = useMemo(
    () => [...(catalog?.folders ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [catalog],
  );
  const tags = useMemo(() => [...tagCounts.keys()].sort(), [tagCounts]);

  // New folder (visible at the top of the panel).
  const [newName, setNewName] = useState("");
  const createMut = useMutation({
    mutationFn: (name: string) => api.createFolder(name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["folders"] });
      setNewName("");
    },
  });
  async function doCreateFolder() {
    const name = newName.trim();
    if (!name) return;
    await createMut.mutateAsync(name);
  }

  // Rename / delete — inline on every label row.
  const [renaming, setRenaming] = useState<{ kind: LabelKind; name: string } | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const renameMut = useMutation({
    mutationFn: (v: { kind: LabelKind; from_: string; to: string }) =>
      api.renameOrganize(v.kind, v.from_, v.to),
    onSuccess: () => invalidateAll(),
  });
  const clearMut = useMutation({
    mutationFn: (v: { kind: LabelKind; value: string }) =>
      api.clearOrganize(v.kind, v.value),
    onSuccess: () => invalidateAll(),
  });

  function startRename(kind: LabelKind, name: string) {
    setRenaming({ kind, name });
    setRenameValue(name);
  }
  async function doRename() {
    if (!renaming) return;
    const to = renameValue.trim();
    const from = renaming.name;
    if (to && to !== from) {
      // Renaming the folder you are CURRENTLY viewing: follow it, else the URL
      // would point at a name that no longer exists (empty view).
      if (renaming.kind === "folder" && from === activeFolder) onSelectFolder(to);
      await renameMut.mutateAsync({ kind: renaming.kind, from_: from, to });
    }
    setRenaming(null);
  }
  async function doDelete(kind: LabelKind, name: string) {
    if (!window.confirm(`'${name}' ${kind === "folder" ? "folder" : "tag"} saari leads se hatayein?`)) {
      return;
    }
    if (kind === "folder" && name === activeFolder) onSelectFolder(""); // leave the deleted view
    await clearMut.mutateAsync({ kind, value: name });
  }

  function selectFolder(name: string) {
    onSelectFolder(name);
    onClose();
  }

  if (!open) return null;

  function labelRow(
    key: string,
    kind: LabelKind,
    label: string,
    count: number,
    active: boolean,
    onOpen: () => void,
  ) {
    if (renaming?.kind === kind && renaming.name === key) {
      return (
        <div key={key} className="flex items-center gap-1 rounded-lg bg-black/25 px-1.5 py-1">
          <input
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doRename()}
            autoFocus
            className="min-w-0 flex-1 rounded-md bg-white/[0.06] px-2 py-1 text-[12.5px] text-white outline-none focus:ring-2 focus:ring-indigo-500/50"
          />
          <button
            onClick={doRename}
            title="Save rename"
            className="rounded p-1 text-indigo-300 hover:text-indigo-100"
          >
            <Check className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => setRenaming(null)}
            title="Cancel"
            className="rounded p-1 text-slate-400 hover:text-slate-200"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      );
    }
    return (
      <div
        key={key}
        className={`group flex items-center rounded-lg border ${
          active
            ? "border-indigo-500/50 bg-indigo-500/15"
            : "border-transparent hover:bg-white/[0.04]"
        }`}
      >
        <button
          onClick={onOpen}
          disabled={kind === "tag"}
          title={kind === "folder" ? `${label} folder me dekhein` : undefined}
          className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1.5 text-left"
        >
          <span className={kind === "folder" ? "text-amber-300" : "text-indigo-300"}>
            {kind === "folder" ? "📁" : <TagIcon className="w-3.5 h-3.5" />}
          </span>
          <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-slate-100">
            {label}
          </span>
          <span className={`text-[11px] font-semibold ${active ? "text-indigo-200" : "text-slate-400"}`}>
            {count}
          </span>
        </button>
        <span className="flex items-center gap-0.5 pr-1.5 opacity-70 group-hover:opacity-100">
          <button
            onClick={() => startRename(kind, label)}
            title={`Rename ${label}`}
            className="rounded p-1 text-slate-400 hover:bg-white/10 hover:text-indigo-300"
          >
            <Pencil className="w-3 h-3" />
          </button>
          <button
            onClick={() => doDelete(kind, label)}
            title={`Delete ${label} (har lead se)`}
            className="rounded p-1 text-slate-400 hover:bg-white/10 hover:text-rose-300"
          >
            <Trash2 className="w-3 h-3" />
          </button>
        </span>
      </div>
    );
  }

  return (
    <>
      {/* Click-outside closes the menu. */}
      <div className="fixed inset-0 z-40" onClick={onClose} />
      <div
        className="absolute right-0 z-50 mt-2 max-h-[520px] w-80 overflow-y-auto rounded-2xl border border-white/10 bg-[#11151E] p-3 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 className="text-[13px] font-semibold text-white">Manage folders &amp; tags</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-200">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* New folder — always visible, so the user can SEE the folder exists. */}
        <div className="mt-2 flex items-center gap-1.5">
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doCreateFolder()}
            placeholder="Naya folder (e.g. Monday data)…"
            className="min-w-0 flex-1 rounded-lg bg-white/[0.06] px-2.5 py-2 text-[12.5px] text-white outline-none placeholder:text-slate-500 focus:ring-2 focus:ring-indigo-500/50"
          />
          <button
            onClick={doCreateFolder}
            disabled={createMut.isPending || !newName.trim()}
            title="Folder banao (khaali bhi chalega)"
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-indigo-600 text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {createMut.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Plus className="w-4 h-4" />}
          </button>
        </div>

        {/* Folders — the actual mailbox places. */}
        <p className="mt-4 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-indigo-300">
          📁 Folders
        </p>
        <div className="mt-1.5 flex flex-col gap-0.5">
          {folders.map((f) =>
            labelRow(f.name, "folder", f.name, f.count, activeFolder === f.name, () =>
              selectFolder(f.name),
            ),
          )}
          {folders.length === 0 && (
            <p className="px-2 py-1.5 text-[12px] text-slate-500">
              Abhi koi folder nahi — upar naya folder banao, phir leads ko move karo.
            </p>
          )}
        </div>

        {/* Tags — labels that span folders. */}
        <p className="mt-4 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-indigo-300">
          <TagIcon className="w-3.5 h-3.5" /> Tags
        </p>
        <div className="mt-1.5 flex flex-col gap-0.5">
          {tags.map((t) =>
            labelRow(t, "tag", t, tagCounts.get(t) ?? 0, false, () => undefined),
          )}
          {tags.length === 0 && (
            <p className="px-2 py-1.5 text-[12px] text-slate-500">
              Tags kisi row ke Tag button se assign hote hain (e.g. Hot, Texas).
            </p>
          )}
        </div>
      </div>
    </>
  );
}