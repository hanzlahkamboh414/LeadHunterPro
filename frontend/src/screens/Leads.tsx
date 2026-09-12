import { useCallback, useLayoutEffect, useMemo, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useLocation, useSearchParams } from "react-router-dom";
import { Download, CheckSquare, Flame, Gauge, Inbox, Layers, Square, Sprout, Tag, Trash2, UserCheck, X } from "lucide-react";
import { api } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import { Combobox, Select } from "../components/Select";
import { Spinner } from "../components/StatusChip";
import ManageMenu from "../components/ManageMenu";
import DeleteReasonDialog, { type DeleteReason } from "../components/DeleteReasonDialog";
import type { EvidenceFact } from "../types";
import { recommendationBadge, recommendationLabel, scoreColor } from "../lib/format";

// Working-only (CLAUDE.md §1 honest leads): skip/junk dossiers are NOT leads —
// the user asked "frontend pr sirf working emails hi show ho". The backend
// already hides skip by default; removing the option here makes that airtight.
type RecFilter = "" | "contact_now" | "nurture";
type BoundFilter = "" | "true" | "false";

// Scroll + visited persistence.
//
// <main> is the app's scroller (App.tsx pins the shell to h-screen). We still
// check the document element as a fallback: reading the max and writing to both
// is correct whichever one actually scrolls, and writing to a non-scrolling
// element is a harmless no-op. That keeps restoration working if a screen ever
// introduces its own scroll container.
//
// History: while the shell used `min-h-screen` (a MINIMUM), it grew with its
// content, <main> never got a bounded height, its overflow-y-auto did nothing,
// and the WINDOW scrolled — so `main.scrollTop` read 0 forever and every restore
// attempt silently did nothing.
function scrollEls(): HTMLElement[] {
  const els: HTMLElement[] = [];
  const main = document.querySelector("main");
  if (main) els.push(main as HTMLElement);
  const doc = (document.scrollingElement as HTMLElement) || document.documentElement;
  if (doc && doc !== main) els.push(doc);
  return els;
}

function currentScrollTop(): number {
  return scrollEls().reduce((max, el) => Math.max(max, el.scrollTop), 0);
}

// Kept in a module-level variable (not consumed on read): this is an SPA — the
// /leads -> /leads/:email round trip unmounts Leads but never reloads the page.
// It is also StrictMode-safe (dev mounts/unmounts/remounts): a consume-then-
// restore let the throwaway first mount eat the value and left the real one at
// the top. Re-applying the same offset is idempotent.
let savedScrollTop = 0;

const VISITED_KEY = "leads-visited";
// The row the user opened LAST — a distinct "you were here" accent, so coming
// back answers "kis email par click kiya tha" at a glance.
const LAST_OPENED_KEY = "leads-last-opened";

function saveLeadsScroll() {
  savedScrollTop = currentScrollTop();
}

function loadVisited(): Set<string> {
  try {
    const raw = localStorage.getItem(VISITED_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

function loadLastOpened(): string {
  try {
    return localStorage.getItem(LAST_OPENED_KEY) ?? "";
  } catch {
    return "";
  }
}

export default function Leads() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();

  // The URL is the source of truth for filters (so Dashboard stat cards can
  // deep-link straight into a filtered view). Mutators rewrite the URL.
  const rec = (params.get("recommendation") as RecFilter) ?? "";
  const bound: BoundFilter =
    params.get("bound") === "true"
      ? "true"
      : params.get("bound") === "false"
        ? "false"
        : "";
  const minScore = params.get("min_score") ?? "";
  const src = (params.get("source") ?? "").trim();
  const q = (params.get("q") ?? "").trim().toLowerCase();
  const folder = (params.get("folder") ?? "").trim();
  const tag = (params.get("tag") ?? "").trim();
  const date = (params.get("date") ?? "").trim();
  // A date/tag search is GLOBAL (har folder me dhundhta hai) — no single place
  // is active in the rail while it runs, so the view honestly shows the cross-
  // place recall, not the Unfiled inbox.
  const inGlobalSearch = !folder && (!!date || !!tag);

  function setRec(v: RecFilter) {
    const p = new URLSearchParams(params);
    if (v) p.set("recommendation", v);
    else p.delete("recommendation");
    setParams(p, { replace: true });
  }
  function setBound(v: BoundFilter) {
    const p = new URLSearchParams(params);
    if (v) p.set("bound", v);
    else p.delete("bound");
    setParams(p, { replace: true });
  }
  function setMinScore(v: string) {
    const p = new URLSearchParams(params);
    if (v) p.set("min_score", v);
    else p.delete("min_score");
    setParams(p, { replace: true });
  }
  function setSource(v: string) {
    const p = new URLSearchParams(params);
    if (v) p.set("source", v);
    else p.delete("source");
    setParams(p, { replace: true });
  }
  function setFolder(v: string) {
    const p = new URLSearchParams(params);
    if (v) p.set("folder", v);
    else p.delete("folder");
    setParams(p, { replace: true });
  }
  function setTag(v: string) {
    const p = new URLSearchParams(params);
    if (v) p.set("tag", v);
    else p.delete("tag");
    setParams(p, { replace: true });
  }
  function setDate(v: string) {
    const p = new URLSearchParams(params);
    if (v) p.set("date", v);
    else p.delete("date");
    setParams(p, { replace: true });
  }

  // Set a PLACE (folder) + optional tag ATOMICALLY in one URL write — chip
  // clicks must never leave a stale folder while a tag is applied (two separate
  // setters would race on the same base params). Date is cleared because a
  // place switch resets the cross-place recall too.
  function setPlace(folderValue: string, tagValue: string) {
    const p = new URLSearchParams(params);
    if (folderValue) p.set("folder", folderValue);
    else p.delete("folder");
    if (tagValue) p.set("tag", tagValue);
    else p.delete("tag");
    p.delete("date");
    setParams(p, { replace: true });
  }
  const [manageOpen, setManageOpen] = useState(false);

  // The Companies screen pages IN THE DATABASE (Phase 2 — server speed): the
  // backend filters, sorts and paginates in SQL, so only ONE page is downloaded
  // (never the whole store). q is answered server-side now, so the queryKey
  // carries it and the old client-side filter memo is gone.
  const PAGE = 100;
  const pageQ = useInfiniteQuery({
    queryKey: ["leads", rec, bound, minScore, src, folder, tag, date, q],
    queryFn: ({ pageParam = 0 }) =>
      api.pageLeads({
        recommendation: rec || undefined,
        bound: bound === "" ? undefined : bound === "true",
        min_score: minScore === "" ? undefined : Number(minScore),
        source: src || undefined,
        folder: folder || undefined,
        tag: tag || undefined,
        date: date || undefined,
        q: q || undefined,
        limit: PAGE,
        offset: pageParam,
      }),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((n, p) => n + p.rows.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
  });
  const { isLoading, isError, error, isFetching, refetch } = pageQ;
  const leads = useMemo(
    () => pageQ.data?.pages.flatMap((p) => p.rows) ?? [],
    [pageQ.data],
  );
  // Honest total for the CURRENT filters (from X-Total-Count), so "Show more"
  // knows when it's done and the header can be accurate even on page 1.
  const totalLeads = pageQ.data?.pages[0]?.total ?? 0;

  // Stat-strip tallies — computed from the LOADED rows only (the backend
  // pages in SQL), so a filtered view with 500 matches still shows honest
  // numbers for the first page, never a fake full-store count.
  const contactNowCount = leads.filter((l) => l.recommendation === "contact_now").length;
  const nurtureCount = leads.filter((l) => l.recommendation === "nurture").length;
  const boundCount = leads.filter((l) => l.bound).length;
  const avgScore = leads.length ? leads.reduce((s, l) => s + l.score, 0) / leads.length : 0;

  // Folders catalog (Phase B.2) — persisted, empty-allowed clickable groups.
  // The filter + manage panel read from here so a folder exists BEFORE any
  // lead is in it ("pehle folder banao, phir leads move karo").
  const foldersQ = useQuery({
    queryKey: ["folders"],
    queryFn: () => api.listFolders(),
  });
  // Global extraction-date options (every dossier, folders included) — the
  // "kis tareekh ko kya nikla" dropdown reads from here, not the current page,
  // so a folderized lead's date stays selectable.
  const datesQ = useQuery({
    queryKey: ["dates"],
    queryFn: () => api.listDates(),
  });
  // Global tag COUNTS — one SQL GROUP BY via /leads/tags, so the tag chips +
  // Manage menu + tag dropdown never download the whole list just to tally
  // tags (the old tagsQ pulled folder="*" limit=1000). Shares the ["leads"]
  // prefix so organize/delete still auto-refreshes it.
  const tagsQ = useQuery({
    queryKey: ["leads", { scope: "org-tags" }],
    queryFn: () => api.listTags(),
  });
  const globalTagCounts = useMemo(() => {
    const m = new Map<string, number>();
    (tagsQ.data ?? []).forEach((t) => m.set(t.tag, t.count));
    return m;
  }, [tagsQ.data]);
  const globalTags = useMemo(() => [...globalTagCounts.keys()].sort(), [globalTagCounts]);
  // Every query-run source label — /leads/sources (the old sourceOptions read
  // them off the downloaded page; now they arrive complete in one call).
  const sourcesQ = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.listSources(),
  });
  const sourceOptions = useMemo(() => sourcesQ.data ?? [], [sourcesQ.data]);

  const [expandedEmail, setExpandedEmail] = useState<string | null>(null);
  const detailQ = useQuery({
    queryKey: ["lead", expandedEmail],
    queryFn: () => (expandedEmail ? api.getLead(expandedEmail) : null),
    enabled: !!expandedEmail,
  });

  // --- Data management (user-controlled): users can dismiss junk/dead leads. ---
  const qc = useQueryClient();
  const [busyEmail, setBusyEmail] = useState<string | null>(null);
  const [junkNote, setJunkNote] = useState("");
  // Pending delete: a single email, or "*" for the whole bulk selection. The
  // reason dialog must answer BEFORE anything is deleted (mandatory reason).
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [bulkDeleteBusy, setBulkDeleteBusy] = useState(false);
  const del = useMutation({
    mutationFn: (v: { email: string; reason: DeleteReason }) =>
      api.deleteLead(v.email, v.reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["leads"] });
      qc.invalidateQueries({ queryKey: ["folders"] });
    },
  });
  const clearJunk = useMutation({
    mutationFn: () => api.clearJunk(),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["leads"] });
      qc.invalidateQueries({ queryKey: ["folders"] });
      setJunkNote(
        res.removed > 0
          ? `Clear junk: ${res.removed} dead/junk lead(s) removed.`
          : "Clear junk: koi junk lead nahi mili (sab actionable hain).",
      );
    },
  });

  function handleDelete(email: string) {
    setPendingDelete(email); // the mandatory-reason dialog decides from here
  }

  async function confirmDelete(reason: DeleteReason) {
    if (pendingDelete === "*") {
      // Bulk: the chosen reason applies to every selected lead.
      const emails = [...selected];
      if (emails.length > 0) {
        setBulkDeleteBusy(true);
        try {
          await Promise.allSettled(
            emails.map((email) => api.deleteLead(email, reason)),
          );
          setSelected(new Set());
        } finally {
          setBulkDeleteBusy(false);
          qc.invalidateQueries({ queryKey: ["leads"] });
          qc.invalidateQueries({ queryKey: ["folders"] });
        }
      }
      setPendingDelete(null);
      return;
    }
    if (pendingDelete) {
      setBusyEmail(pendingDelete);
      try {
        await del.mutateAsync({ email: pendingDelete, reason });
      } finally {
        setBusyEmail(null);
      }
      setPendingDelete(null);
    }
  }

  async function handleClearJunk() {
    if (!window.confirm("Saari junk leads delete karein?\n\nIska matlab: hidden skip/dead-domain dossiers (jinke paas dead domain, non-construction, ya score < 3.0 hain) remove ho jayengi.")) {
      return;
    }
    setJunkNote("");
    await clearJunk.mutateAsync();
  }

  // --- Organization (Phase B): folders + tags per lead, bulk assign, manage. ---
  const organize = useMutation({
    mutationFn: (v: { email: string; folder: string; tags: string[] }) =>
      api.organizeLead(v.email, { folder: v.folder, tags: v.tags }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["leads"] });
      qc.invalidateQueries({ queryKey: ["folders"] }); // folder counts changed
    },
  });

  const [editEmail, setEditEmail] = useState<string | null>(null);
  const [editFolder, setEditFolder] = useState("");
  const [editTags, setEditTags] = useState("");
  function startEdit(email: string) {
    const lead = leads.find((l) => l.email === email);
    setEditEmail(email);
    setEditFolder(lead?.folder ?? "");
    setEditTags((lead?.tags ?? []).join(", "));
  }
  async function saveEdit() {
    if (!editEmail) return;
    const tags = editTags.split(",").map((s) => s.trim()).filter(Boolean);
    await organize.mutateAsync({ email: editEmail, folder: editFolder.trim(), tags });
    setEditEmail(null);
  }

  const [bulkFolder, setBulkFolder] = useState("");
  const [bulkTagsIn, setBulkTagsIn] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);
  async function applyBulk() {
    if (selected.size === 0) return;
    const addTags = bulkTagsIn.split(",").map((s) => s.trim()).filter(Boolean);
    const folderToSet = bulkFolder.trim();
    setBulkBusy(true);
    try {
      await Promise.allSettled(
        [...selected].map((email) => {
          const lead = leads.find((l) => l.email === email);
          const merged = [...new Set([...(lead?.tags ?? []), ...addTags])];
          return organize.mutateAsync({
            email,
            folder: folderToSet || (lead?.folder ?? ""),
            tags: merged,
          });
        }),
      );
      setBulkFolder("");
      setBulkTagsIn("");
    } finally {
      setBulkBusy(false);
    }
  }

  function handleBulkDelete() {
    if (selected.size === 0) return;
    setPendingDelete("*"); // bulk marker — dialog asks ONE reason for all
  }

  // Folder/tag rename + delete + new-folder creation live in the ⚙ Manage
  // dropdown (each label row has its own ✏️/🗑 buttons); browsing lives in the
  // chip bar above the table.

  // Folder + tag names for the assign inputs' datalists (the catalog feeds the
  // bulk bar + row editor suggestions; the sidebar renders the rail itself).
  const catalog = foldersQ.data;
  const folderList = useMemo(
    () => (catalog?.folders ?? []).slice().sort((a, b) => a.name.localeCompare(b.name)),
    [catalog],
  );
  // Tag counts/options for the Tag dropdown + tag datalist — GLOBAL now (the
  // /leads/tags group), so the dropdown lists ALL tags, not just the current
  // page (the old page-derived map hid tags once paging arrived).
  const tagCounts = globalTagCounts;
  const uniqueTags = useMemo(() => [...globalTagCounts.keys()].sort(), [globalTagCounts]);
  // Distinct extraction dates — GLOBAL (every dossier, folders included). The
  // loaded page is the fallback so the dropdown is never empty while the
  // catalog is still loading.
  const uniqueDates = useMemo(() => {
    const fromApi = datesQ.data ?? [];
    if (fromApi.length > 0) return fromApi;
    return [...new Set(leads.map((l) => l.created_at).filter(Boolean))].sort(
      (a, b) => b.localeCompare(a),
    );
  }, [datesQ.data, leads]);

  // --- Selection state ---
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Select-all mirrors the LOADED rows (page by page), so check-all stays honest
  // as "Show more" reveals more — the user picks from what is actually on screen.

  function toggleSelect(email: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(email)) next.delete(email);
      else next.add(email);
      return next;
    });
  }

  function toggleSelectAll() {
    if (selected.size === leads.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(leads.map((l) => l.email)));
    }
  }

  // --- Visited marker + last-opened accent + scroll restoration ---
  const [visited, setVisited] = useState<Set<string>>(loadVisited);
  const [lastOpened, setLastOpened] = useState<string>(loadLastOpened);

  function markVisited(email: string) {
    setLastOpened(email);
    try {
      localStorage.setItem(LAST_OPENED_KEY, email);
    } catch {
      /* best-effort */
    }
    setVisited((prev) => {
      if (prev.has(email)) return prev;
      const next = new Set(prev);
      next.add(email);
      try {
        localStorage.setItem(VISITED_KEY, JSON.stringify([...next]));
      } catch {
        /* best-effort */
      }
      return next;
    });
  }

  // Returning from a lead detail: put the list back exactly where the user
  // clicked. It RETRIES across frames because the browser clamps scrollTop to
  // the current content height — on mount the table is often still shorter than
  // the target (refetch, wrapped rows), so a single assignment lands short.
  useLayoutEffect(() => {
    if (isLoading || savedScrollTop <= 0) return;
    const target = savedScrollTop;
    let frames = 0;
    let raf = 0;
    const apply = () => {
      scrollEls().forEach((el) => {
        el.scrollTop = target;
      });
      // Stop as soon as it holds; keep trying while the list is still growing.
      if (Math.abs(currentScrollTop() - target) > 2 && frames++ < 60) {
        raf = requestAnimationFrame(apply);
      }
    };
    apply();
    return () => cancelAnimationFrame(raf);
  }, [isLoading]);

  // Export runs SERVER-SIDE (Phase 2): the CSV honours the exact filters the
  // Companies list shows and exports the WHOLE matching set — the downloadable
  // file no longer depends on how many pages happen to be loaded on screen.
  const exportSelected = useCallback(async () => {
    if (selected.size === 0) return;
    try {
      const csv = await api.exportCsvData({
        emails: [...selected],
        folder: folder || undefined,
      });
      downloadCsvText(csv, `leads-selected-${selected.size}.csv`);
    } catch {
      window.alert("Export selected done nahi ho saka (server ne CSV nahi diya).");
    }
  }, [selected, folder]);

  const exportAll = useCallback(async () => {
    try {
      const csv = await api.exportCsvData({
        recommendation: rec === "contact_now" || rec === "nurture" ? rec : undefined,
        folder: folder || undefined,
        tag: tag || undefined,
        date: date || undefined,
        source: src || undefined,
        bound: bound === "" ? undefined : bound === "true",
        min_score: minScore === "" ? undefined : Number(minScore),
        q: q || undefined,
      });
      downloadCsvText(csv, `leads-${rec || "all"}.csv`);
    } catch {
      window.alert("Export done nahi ho saka (server ne CSV nahi diya).");
    }
  }, [rec, folder, tag, date, src, bound, minScore, q]);

  return (
    <div className="px-8 py-7 max-w-7xl">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <PageHeader
          eyebrow="LeadHunter Pro"
          title={q ? `Results for "${params.get("q")}"` : "Companies"}
          subtitle="Researched companies with a bound decision-maker and a buying-window score."
        />
        <div className="flex items-center gap-2">
          {selected.size > 0 && (
            <button
              onClick={exportSelected}
              className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-medium text-white hover:bg-indigo-500"
            >
              <Download className="w-4 h-4" />
              Export Selected ({selected.size})
            </button>
          )}
          <button
            onClick={exportAll}
            className="inline-flex items-center gap-2 rounded-lg border border-white/5 px-4 py-2 text-[13px] text-slate-300 hover:bg-white/[0.04]"
          >
            <Download className="w-4 h-4" /> Export All
          </button>
          <button
            onClick={handleClearJunk}
            disabled={clearJunk.isPending}
            className="inline-flex items-center gap-2 rounded-lg border border-rose-400/20 bg-rose-500/10 px-4 py-2 text-[13px] text-rose-300 hover:bg-rose-500/20 disabled:opacity-50"
          >
            {clearJunk.isPending ? <Spinner className="h-4 w-4" /> : <Trash2 className="w-4 h-4" />}
            Clear junk
          </button>
        </div>
      </div>

      {/* Mini stat strip — the current view at a glance, Dashboard-style.
          Clickable cards deep-link into the matching filter (toggle off on
          second click). Counts are from the loaded rows; "loaded" appears
          when the server total is larger (honest paging). */}
      {!isLoading && leads.length > 0 && (
        <div className="mt-4 grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatMini
            label="In this view"
            value={totalLeads.toLocaleString()}
            sub={leads.length < totalLeads ? `${leads.length} loaded — Show more for the rest` : "all loaded"}
            icon={Layers}
          />
          <StatMini
            label="Contact Now"
            value={contactNowCount.toLocaleString()}
            sub={`${pctOf(contactNowCount, leads.length)} of loaded`}
            icon={Flame}
            tint="emerald"
            active={rec === "contact_now"}
            onClick={() => setRec(rec === "contact_now" ? "" : "contact_now")}
          />
          <StatMini
            label="Nurture"
            value={nurtureCount.toLocaleString()}
            sub={`${pctOf(nurtureCount, leads.length)} of loaded`}
            icon={Sprout}
            tint="amber"
            active={rec === "nurture"}
            onClick={() => setRec(rec === "nurture" ? "" : "nurture")}
          />
          <StatMini
            label="Avg potential"
            value={avgScore.toFixed(1)}
            sub={`${pctOf(boundCount, leads.length)} decision-maker bound`}
            icon={bound === "true" ? UserCheck : Gauge}
            tint="indigo"
            active={bound === "true"}
            onClick={() => setBound(bound === "true" ? "" : "true")}
          />
        </div>
      )}

      {/* Places chip bar — the folder/tag surface as ONE row above the table
          (Inbox · All · 📁 per folder · # per tag), so no left rail steals data
          width. Create/rename/delete live in the ⚙ Manage dropdown on the
          right — a clean single-row filter, not a sidebar. */}
      <div className="mt-5 flex flex-wrap items-center gap-1.5">
        <PlaceChip
          active={folder === "" && !tag && !date}
          icon={<Inbox className="w-3.5 h-3.5" />}
          onClick={() => setPlace("", "")}
          title="Unfiled inbox — sirf wo leads jo kisi folder me nahi hain"
        >
          Inbox <span className="font-semibold">{catalog?.unfiled ?? 0}</span>
        </PlaceChip>
        <PlaceChip
          active={folder === "*"}
          icon={<Layers className="w-3.5 h-3.5" />}
          onClick={() => setPlace("*", "")}
          title="All — har place ka data (inbox + saare folders)"
        >
          All <span className="font-semibold">{catalog?.total ?? 0}</span>
        </PlaceChip>
        {folderList.length > 0 && <span className="mx-1 h-4 w-px bg-white/10" />}
        {folderList.map((f) => (
          <PlaceChip
            key={f.name}
            active={folder === f.name}
            icon={<span className="text-amber-300">📁</span>}
            onClick={() => setPlace(f.name, "")}
            title={`${f.name} folder ke saare leads`}
          >
            {f.name} <span className="font-semibold">{f.count}</span>
          </PlaceChip>
        ))}
        {globalTags.length > 0 && <span className="mx-1 h-4 w-px bg-white/10" />}
        {globalTags.map((t) => (
          <PlaceChip
            key={t}
            active={tag === t && !folder && !date}
            icon={<Tag className="w-3.5 h-3.5" />}
            onClick={() => setPlace("", t)}
            title={`${t} tag wali saari leads (har folder me)`}
          >
            {t} <span className="font-semibold">{globalTagCounts.get(t) ?? 0}</span>
          </PlaceChip>
        ))}
        <div className="relative ml-auto">
          <button
            onClick={() => setManageOpen((o) => !o)}
            title="Naya folder, rename, delete — saari organization"
            className="inline-flex items-center gap-1.5 rounded-full border border-white/5 bg-white/[0.03] px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.06] hover:text-slate-100"
          >
            ⚙ Manage folders &amp; tags
          </button>
          <ManageMenu
            open={manageOpen}
            onClose={() => setManageOpen(false)}
            catalog={catalog}
            tagCounts={globalTagCounts}
            activeFolder={folder}
            onSelectFolder={(v) => setPlace(v, "")}
          />
        </div>
      </div>
          {junkNote && (
            <p className="mt-3 text-[12.5px] text-emerald-300 bg-emerald-500/10 rounded-lg px-3 py-2">
              {junkNote}
            </p>
          )}

      {/* Bulk organize bar — appears when rows are selected */}
      {selected.size > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-indigo-500/20 bg-indigo-500/10 px-3 py-2.5">
          <span className="text-[12.5px] font-medium text-indigo-200">
            Bulk organize ({selected.size} selected)
          </span>
          <Combobox
            className="w-40"
            value={bulkFolder}
            onChange={setBulkFolder}
            options={folderList.map((f) => f.name)}
            placeholder="Set folder…"
          />
          <input
            value={bulkTagsIn}
            onChange={(e) => setBulkTagsIn(e.target.value)}
            placeholder="Add tags (comma)…"
            className={`${selectCls} w-48`}
          />
          <button
            onClick={applyBulk}
            disabled={bulkBusy || (!bulkFolder.trim() && !bulkTagsIn.trim())}
            className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-2 text-[12.5px] font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {bulkBusy ? <Spinner className="h-3.5 w-3.5" /> : <Tag className="w-3.5 h-3.5" />}
            Apply
          </button>
          <button
            onClick={handleBulkDelete}
            disabled={bulkDeleteBusy}
            className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-2 text-[12.5px] font-medium text-white hover:bg-rose-500 disabled:opacity-50"
          >
            {bulkDeleteBusy ? <Spinner className="h-3.5 w-3.5" /> : <Trash2 className="w-3.5 h-3.5" />}
            Delete selected ({selected.size})
          </button>
        </div>
      )}

      {inGlobalSearch ? (
        <p className="text-[12.5px] text-indigo-300/90">
          🔎 {date ? `Date "${fmtDate(date)}"` : "Tag"} search — har folder me dhundho
          (foldered leads bhi isme aate hain). Zero karne ke liye Clear dabao.
        </p>
      ) : null}
      {folder === "*" ? (
        <p className="text-[12.5px] text-slate-400">
          🗂 All — har place ka data (Unfiled inbox + saare folders). Folder view par
          wapas jaane ke liye upar 📁 folder chips me se apna folder chuno.
        </p>
      ) : null}

      {/* Filters */}
      <div className="mt-4 mb-4 flex flex-wrap items-end gap-3">
        <Filter>
          <span className={labelCls}>Recommendation</span>
          <Select
            className="w-36"
            value={rec}
            onChange={(v) => setRec(v as RecFilter)}
            options={[
              { value: "", label: "Actionable" },
              { value: "contact_now", label: "Contact Now" },
              { value: "nurture", label: "Nurture" },
            ]}
          />
        </Filter>
        <Filter>
          <span className={labelCls}>Decision-maker</span>
          <Select
            className="w-32"
            value={bound}
            onChange={(v) => setBound(v as BoundFilter)}
            options={[
              { value: "", label: "All" },
              { value: "true", label: "Bound" },
              { value: "false", label: "Unbound" },
            ]}
          />
        </Filter>
        <Filter>
          <span className={labelCls}>Min score</span>
          <input
            type="number"
            min={0}
            max={10}
            value={minScore}
            onChange={(e) => setMinScore(e.target.value)}
            placeholder="Any"
            className={`${selectCls} w-24`}
          />
        </Filter>
        <Filter>
          <span className={labelCls}>Run / source</span>
          <Select
            className="w-52"
            value={src}
            onChange={setSource}
            placeholder="All runs"
            options={sourceOptions.map((s) => ({ value: s, label: s }))}
          />
        </Filter>
        <Filter>
          <span className={labelCls}>Tag</span>
          <Select
            className="w-44"
            value={tag}
            onChange={setTag}
            placeholder="All"
            options={uniqueTags.map((t) => ({
              value: t,
              label: `${t} (${tagCounts.get(t)})`,
            }))}
          />
        </Filter>
        <Filter>
          <span className={labelCls}>Extracted</span>
          <Select
            className="w-40"
            value={date}
            onChange={setDate}
            placeholder="All dates"
            options={uniqueDates.map((d) => ({ value: d, label: fmtDate(d) }))}
          />
        </Filter>
        <button
          onClick={() => refetch()}
          className="rounded-lg border border-white/5 px-3 py-2 text-[13px] text-slate-300 hover:bg-white/[0.04] inline-flex items-center gap-1"
        >
          {isFetching ? <Spinner className="h-4 w-4" /> : "↻"} Refresh
        </button>
        {(folder || tag || date) && (
          <button
            onClick={() => {
              const p = new URLSearchParams(params);
              p.delete("folder");
              p.delete("tag");
              p.delete("date");
              setParams(p, { replace: true });
            }}
            className="inline-flex items-center gap-1 rounded-lg border border-white/5 px-3 py-2 text-[13px] text-slate-400 hover:text-slate-200 hover:bg-white/[0.04]"
          >
            <X className="w-3.5 h-3.5" /> Clear
          </button>
        )}
      </div>

      {isError && (
        <p className="text-[13px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2">
          Failed to load leads: {(error as Error).message}
        </p>
      )}

      {isLoading ? (
        <div className="flex items-center gap-2 text-slate-500 text-sm py-16 justify-center">
          <Spinner /> Loading leads…
        </div>
      ) : leads.length === 0 ? (
        <div className="rounded-xl border border-dashed border-white/10 py-16 text-center text-slate-500 max-w-2xl mx-auto">
          {!folder && !inGlobalSearch ? (
            <>
              Unfiled inbox khali hai — yahan sirf wohi companies dikhti hain
              jo kisi folder me nahi gayin.{" "}
              <button
                onClick={() => navigate("/research")}
                className="text-indigo-400 hover:underline"
              >
                Research
              </button>{" "}
              par naya search chalao, ya <span className="text-amber-300">upar
              📁 folder chip</span> par click kar ke filed data dekho.
            </>
          ) : folder === "*" ? (
            <>
              Abhi koi lead research nahi hui.{" "}
              <button
                onClick={() => navigate("/research")}
                className="text-indigo-400 hover:underline"
              >
                Research
              </button>{" "}
              screen par naya search chalao.
            </>
          ) : (
            <>
              Is place / filter me koi lead match nahi huwa — koi aur folder{" "}
              <button
                onClick={() => setFolder("")}
                className="text-indigo-400 hover:underline"
              >
                Unfiled
              </button>{" "}
              par click kro, ya{" "}
              <button
                onClick={() => navigate("/research")}
                className="text-indigo-400 hover:underline"
              >
                Research
              </button>{" "}
              par naya search chalao.
            </>
          )}
        </div>
      ) : (
        <>
        <div className="overflow-x-auto rounded-xl border border-white/5">
          <table className="w-full text-[13.5px]">
            <thead>
              <tr className="bg-white/[0.02] text-left text-[12px] uppercase tracking-wide text-slate-500">
                <th className="px-4 py-3 font-medium w-10">
                  <button onClick={toggleSelectAll} className="flex items-center justify-center">
                    {selected.size === leads.length && leads.length > 0 ? (
                      <CheckSquare className="w-4 h-4 text-indigo-400" />
                    ) : (
                      <Square className="w-4 h-4" />
                    )}
                  </button>
                </th>
                <th className="px-4 py-3 font-medium">Company</th>
                <th className="px-4 py-3 font-medium">Person</th>
                <th className="px-4 py-3 font-medium">LinkedIn</th>
                <th className="px-4 py-3 font-medium">Labels</th>
                <th className="px-4 py-3 font-medium">Extracted</th>
                <th className="px-4 py-3 font-medium">Score</th>
                <th className="px-4 py-3 font-medium">Recommendation</th>
                <th className="px-4 py-3 font-medium">Reason</th>
                <th className="px-4 py-3 font-medium w-20" />
              </tr>
            </thead>
            <tbody>
              {leads.map((l) => {
                const expanded = expandedEmail === l.email;
                const isChecked = selected.has(l.email);
                const isVisited = visited.has(l.email);
                const isLast = lastOpened === l.email;
                return (
                  <tr
                    key={l.email}
                    onClick={() => {
                      markVisited(l.email);
                      saveLeadsScroll();
                      // Carry the list's OWN url (filters included) so the detail
                      // screen's back link returns to this folder/tag/date view
                      // instead of the bare Inbox.
                      navigate(`/leads/${encodeURIComponent(l.email)}`, {
                        state: { from: location.pathname + location.search },
                      });
                    }}
                    /* Visited rows get a real marker (tint + ✓ badge), not just
                       dimming — a faded row reads as broken data instead of
                       "already seen". The LAST opened row also gets an indigo
                       left accent: "you were here" when you come back. */
                    className={`cursor-pointer align-top border-t border-white/5 hover:bg-white/[0.03] ${
                      isChecked
                        ? "bg-indigo-500/[0.06]"
                        : isLast
                          ? "bg-indigo-500/[0.09]"
                          : isVisited
                            ? "bg-emerald-500/[0.05]"
                            : ""
                    }`}
                  >
                    <td className="px-4 py-3">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          toggleSelect(l.email);
                        }}
                        className="flex items-center justify-center"
                      >
                        {isChecked ? (
                          <CheckSquare className="w-4 h-4 text-indigo-400" />
                        ) : (
                          <Square className="w-4 h-4 text-slate-500" />
                        )}
                      </button>
                    </td>
                    <td
                      className={`px-4 py-3 max-w-[240px] border-l-2 ${
                        isLast ? "border-indigo-400" : "border-transparent"
                      }`}
                    >
                      <div className="flex items-center gap-1.5 font-medium text-white">
                        {isLast ? (
                          <span
                            className="inline-flex h-4 shrink-0 items-center rounded bg-indigo-500/30 px-1 text-[9px] font-semibold uppercase tracking-wide text-indigo-200"
                            title="Ye wohi lead hai jise aap ne last khola tha"
                          >
                            last
                          </span>
                        ) : (
                          isVisited && (
                            <span
                              className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-emerald-500/25 text-[10px] text-emerald-300"
                              title="Aap is lead ka detail dekh chuke hain"
                            >
                              ✓
                            </span>
                          )
                        )}
                        <span className="truncate">{l.company || "—"}</span>
                      </div>
                      <div className="truncate text-[12px] text-slate-500">{l.email}</div>
                      {l.source && (
                        <span className="mt-1 inline-flex max-w-full items-center truncate rounded-md border border-indigo-500/20 bg-indigo-500/10 px-1.5 py-0.5 text-[10.5px] text-indigo-300">
                          🔍 {l.source}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="text-slate-200">{l.person || "—"}</div>
                      <div className="text-[12px] text-slate-400">{l.role || "role unknown"}</div>
                    </td>
                    <td className="px-4 py-3 max-w-[150px]">
                      {l.linkedin ? (
                        <a
                          href={/^https?:\/\//i.test(l.linkedin) ? l.linkedin : `https://${l.linkedin}`}
                          target="_blank"
                          rel="noreferrer"
                          onClick={(e) => e.stopPropagation()}
                          title={l.linkedin}
                          /* truncate, NOT break-all: a long /in/ URL used to wrap
                             one word-fragment per line and stretch the row. */
                          className="block truncate text-[12px] text-indigo-400 hover:underline"
                        >
                          {l.linkedin}
                        </a>
                      ) : null}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap gap-1 max-w-[200px]">
                        {l.folder && (
                          <button
                            onClick={(ev) => {
                              ev.stopPropagation();
                              setFolder(l.folder);
                            }}
                            title={`${l.folder} folder me is type ka data dekhein`}
                            className="inline-flex items-center rounded-md border border-amber-400/30 bg-amber-500/10 px-1.5 py-0.5 text-[10.5px] text-amber-300 hover:bg-amber-500/20"
                          >
                            📁 {l.folder}
                          </button>
                        )}
                        {l.tags.slice(0, 3).map((t) => (
                          <span
                            key={t}
                            className="inline-flex items-center rounded-md border border-indigo-500/25 bg-indigo-500/10 px-1.5 py-0.5 text-[10.5px] text-indigo-300"
                          >
                            {t}
                          </span>
                        ))}
                        {l.tags.length > 3 && (
                          <span className="inline-flex items-center rounded-md border border-white/10 px-1.5 py-0.5 text-[10.5px] text-slate-500">
                            +{l.tags.length - 3}
                          </span>
                        )}
                        {!l.folder && l.tags.length === 0 && (
                          <span className="text-[12px] text-slate-600">—</span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      {l.created_at ? (
                        <span className="inline-flex items-center rounded-md border border-white/10 px-1.5 py-0.5 text-[10.5px] text-slate-400 whitespace-nowrap">
                          📅 {fmtDate(l.created_at)}
                        </span>
                      ) : (
                        <span className="text-[12px] text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className={`font-semibold ${scoreColor(l.score)}`}>{l.score.toFixed(1)}</div>
                      {/* Quality bar — the number gets a proportional visual so
                          strong rows pop while scanning, weak ones don't. */}
                      <div className="mt-1.5 h-1 w-12 rounded-full bg-white/[0.06] overflow-hidden">
                        <div
                          className={`h-full rounded-full ${
                            l.score >= 7 ? "bg-emerald-300" : l.score >= 4 ? "bg-amber-300" : "bg-slate-400"
                          }`}
                          style={{ width: `${Math.min(100, Math.max(0, l.score * 10))}%` }}
                        />
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <Badge value={l.recommendation} />
                    </td>
                    <td className="px-4 py-3 text-slate-300 w-[280px] max-w-[280px]">
                      {/* Collapsed by default: ONE line, so every row is the
                          same height and the table stays scannable. The full
                          reason + proof links open on click — the reason used to
                          render in full and blow rows up to ~500px. */}
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setExpandedEmail(expanded ? null : l.email);
                        }}
                        className="w-full text-left"
                        title={expanded ? "Chhupao" : l.reason || ""}
                      >
                        {!expanded && (
                          <span className="block text-[12.5px] line-clamp-1">
                            {l.reason || "—"}
                          </span>
                        )}
                        {l.reason && (
                          <span className="mt-0.5 inline-block text-[11px] text-indigo-400 hover:underline">
                            {expanded ? "▲ chhupao" : "▼ poora reason"}
                          </span>
                        )}
                      </button>
                      {expanded && (
                        <div className="mt-2 border-t border-white/5 pt-2">
                          <p className="text-[12.5px] text-slate-200">{l.reason || "—"}</p>
                          <div className="mt-2 border-t border-white/5 pt-2">
                            {detailQ.isLoading ? (
                              <Spinner className="h-3 w-3" />
                            ) : (
                              <ProofLinks
                                evidence={detailQ.data?.intent?.evidence ?? []}
                                fallback={detailQ.data?.intent?.reason ?? l.reason}
                              />
                            )}
                          </div>
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          startEdit(l.email);
                        }}
                        aria-label={`Organize ${l.email}`}
                        title="Set folder / tags"
                        className="inline-flex items-center justify-center rounded-lg border border-white/5 p-1.5 text-slate-500 hover:text-amber-300 hover:border-amber-400/30"
                      >
                        <Tag className="w-4 h-4" />
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          handleDelete(l.email);
                        }}
                        disabled={busyEmail === l.email}
                        aria-label={`Delete ${l.email}`}
                        title="Delete (data management)"
                        className="ml-1 inline-flex items-center justify-center rounded-lg border border-white/5 p-1.5 text-slate-500 hover:text-rose-300 hover:border-rose-400/30 disabled:opacity-50"
                      >
                        {busyEmail === l.email ? (
                          <Spinner className="h-4 w-4" />
                        ) : (
                          <Trash2 className="w-4 h-4" />
                        )}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        {/* Show more — the backend pages in SQL (limit 100), so only one page is
            downloaded at a time (the root cause of the slow screen was the whole
            store + 4 full scans). Revealed only while another page exists. */}
        {pageQ.hasNextPage && (
          <div className="mt-4 flex justify-center">
            <button
              onClick={() => pageQ.fetchNextPage()}
              disabled={pageQ.isFetchingNextPage}
              className="inline-flex items-center gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-5 py-2.5 text-[13px] text-slate-200 hover:bg-white/[0.06] disabled:opacity-60"
            >
              {pageQ.isFetchingNextPage ? <Spinner className="h-4 w-4" /> : "↓"}
              Show more ({leads.length} / {totalLeads})
            </button>
          </div>
        )}
        </>
      )}

      {/* Organize one lead — modal editor */}
      {editEmail && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
          onClick={() => setEditEmail(null)}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-white/10 bg-[#11151E] p-5 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <h2 className="text-[15px] font-semibold text-white">Organize lead</h2>
              <button onClick={() => setEditEmail(null)} className="text-slate-500 hover:text-slate-200">
                <X className="w-4 h-4" />
              </button>
            </div>
            <p className="mt-1 text-[12.5px] text-slate-500 break-all">{editEmail}</p>
            <label className={`${labelCls} block mt-4 mb-1`}>Folder</label>
            <Combobox
              value={editFolder}
              onChange={setEditFolder}
              options={folderList.map((f) => f.name)}
              placeholder="e.g. Hot, Texas, No folder…"
            />
            <label className={`${labelCls} block mt-3 mb-1`}>Tags (comma separated)</label>
            <input
              value={editTags}
              onChange={(e) => setEditTags(e.target.value)}
              placeholder="Hot, Texas, Follow-up"
              className={selectCls}
            />
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setEditEmail(null)}
                className="rounded-lg border border-white/5 px-4 py-2 text-[13px] text-slate-300 hover:bg-white/[0.04]"
              >
                Cancel
              </button>
              <button
                onClick={saveEdit}
                disabled={organize.isPending}
                className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                {organize.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Tag className="w-3.5 h-3.5" />}
                Save
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Mandatory structured reason before any delete (single or bulk) —
          the AI learning gate. count=selected.size for the bulk marker "*". */}
      <DeleteReasonDialog
        count={pendingDelete === "*" ? selected.size : pendingDelete ? 1 : 0}
        busy={del.isPending || bulkDeleteBusy}
        onClose={() => setPendingDelete(null)}
        onConfirm={confirmDelete}
      />
    </div>
  );
}

function ProofLinks({
  evidence,
  fallback,
}: {
  evidence: EvidenceFact[];
  fallback?: string;
}) {
  const links = evidence.filter((e) => e.source_url);
  if (links.length === 0) {
    return (
      <p className="text-[12px] text-slate-600">
        {fallback ? "No source link recorded." : "No proof links."}
      </p>
    );
  }
  // Dedupe by URL: many facts from the same page (e.g. one LinkedIn profile)
  // show the link ONCE with their claims stacked beneath, so the proof block
  // doesn't repeat the same URL for every fact.
  const byUrl = new Map<string, string[]>();
  for (const e of links) {
    if (!byUrl.has(e.source_url)) byUrl.set(e.source_url, []);
    byUrl.get(e.source_url)!.push(e.claim);
  }
  return (
    <ul className="flex flex-col gap-1.5">
      {Array.from(byUrl.entries()).map(([url, claims], i) => (
        <li key={i}>
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            onClick={(ev) => ev.stopPropagation()}
            className="text-[12px] text-indigo-400 hover:underline break-all"
          >
            {url}
          </a>
          {claims.filter(Boolean).map((c, j) => (
            <p key={j} className="text-[11px] text-slate-500 mt-0.5">{c}</p>
          ))}
        </li>
      ))}
    </ul>
  );
}

function Badge({ value }: { value: string }) {
  const b = recommendationBadge(value);
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] ${b.cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${b.dot}`} />
      {recommendationLabel(value)}
    </span>
  );
}

/** Blob-download CSV text the server already rendered (honours the server's
 * exact filters + BOM). The client no longer re-derives rows locally. */
function downloadCsvText(text: string, filename: string) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function Filter({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-col gap-1">{children}</div>;
}

function pctOf(n: number, total: number): string {
  if (!total) return "0%";
  return `${Math.round((n / total) * 100)}%`;
}

/** Mini stat card for the Leads stat strip — same visual language as the
 *  Dashboard cards (border/white-2% surface, icon chip, big value), but
 *  compact. Clickable cards toggle the matching filter. */
function StatMini({
  label,
  value,
  sub,
  icon: Icon,
  tint = "slate",
  active = false,
  onClick,
}: {
  label: string;
  value: string;
  sub: string;
  icon: typeof Layers;
  tint?: "emerald" | "amber" | "indigo" | "slate";
  active?: boolean;
  onClick?: () => void;
}) {
  const tintCls =
    tint === "emerald"
      ? "bg-emerald-500/15 text-emerald-300"
      : tint === "amber"
        ? "bg-amber-500/15 text-amber-300"
        : tint === "indigo"
          ? "bg-indigo-500/15 text-indigo-400"
          : "bg-slate-500/15 text-slate-400";
  const cls = `rounded-xl border p-4 text-left transition-colors ${
    onClick ? "cursor-pointer hover:bg-white/[0.04]" : ""
  } ${active ? "border-indigo-500/50 bg-indigo-500/[0.08]" : "border-white/5 bg-white/[0.02]"}`;
  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[12.5px] text-slate-400">{label}</span>
        <div className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${tintCls}`}>
          <Icon className="h-[15px] w-[15px]" strokeWidth={1.9} />
        </div>
      </div>
      <div className="mt-1.5 text-[22px] font-semibold text-white">{value}</div>
      <div className="mt-0.5 text-[11.5px] text-slate-500">{sub}</div>
    </>
  );
  return onClick ? (
    <button type="button" onClick={onClick} className={cls}>
      {body}
    </button>
  ) : (
    <div className={cls}>{body}</div>
  );
}

function PlaceChip({
  active,
  icon,
  onClick,
  title,
  children,
}: {
  active: boolean;
  icon: React.ReactNode;
  onClick: () => void;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] transition-colors ${
        active
          ? "border-indigo-500/50 bg-indigo-500/20 text-indigo-100"
          : "border-white/5 bg-white/[0.03] text-slate-300 hover:bg-white/[0.06] hover:text-slate-100"
      }`}
    >
      <span className={active ? "text-indigo-200" : "text-slate-400"}>{icon}</span>
      {children}
    </button>
  );
}

/** "YYYY-MM-DD" -> "Sep 1, 2026" (built from parts to avoid UTC shifts). */
function fmtDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
  if (!m) return iso;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

const selectCls =
  "bg-white/[0.04] border border-white/5 rounded-lg px-3 py-2 text-[13px] text-slate-300 outline-none focus:ring-2 focus:ring-indigo-500/40";

const labelCls = "text-[12px] font-medium text-slate-500 uppercase tracking-wide";