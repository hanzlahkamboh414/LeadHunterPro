import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Mail,
  Star,
  Paperclip,
  Search,
  ChevronLeft,
  ChevronRight,
  Trash2,
  Archive,
  Reply,
  Forward,
  Send,
  Download,
  X,
  MailOpen,
  ChevronDown,
  Inbox as InboxIcon,
  FileSpreadsheet,
} from "lucide-react";
import { ApiError, api, googleAuthorizeUrl } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import type { EmailAccount, GmailMessage } from "../types";

const INPUT =
  "w-full bg-white/[0.04] border border-white/5 rounded-lg px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40";

const FOLDERS = [
  { key: "inbox", label: "Inbox", icon: InboxIcon },
  { key: "sent", label: "Sent", icon: Send },
  { key: "starred", label: "Starred", icon: Star },
  { key: "trash", label: "Trash", icon: Trash2 },
];

const YEARS: number[] = [];
for (let y = new Date().getFullYear() + 1; y >= 2004; y--) YEARS.push(y);

/** RFC-2822 Date header → local short form; raw text when unparseable. */
function fmtDate(raw: string): string {
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
    " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/** "Jane <jane@acme.com>" → "Jane" (or the bare address). */
function displayAddress(value: string): string {
  const m = /^(.*?)\s*<([^>]+)>\s*$/.exec(value.trim());
  return m ? (m[1].replace(/^"|"$/g, "") || m[2]) : value.trim();
}

function fmtSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

/** What a Reply pre-fills — the threading headers make it land in the
 * recipient's (and this) thread properly. */
interface ComposeDraft {
  to: string;
  cc: string;
  bcc: string;
  subject: string;
  body: string;
  in_reply_to: string;
  references: string;
  mode: "new" | "reply" | "forward";
}

function emptyDraft(): ComposeDraft {
  return {
    to: "", cc: "", bcc: "", subject: "", body: "",
    in_reply_to: "", references: "", mode: "new",
  };
}

export default function EmailInbox() {
  const queryClient = useQueryClient();
  const [banner, setBanner] = useState<{ ok: boolean; text: string } | null>(null);
  const [accountId, setAccountId] = useState<number | null>(null);
  const [folder, setFolder] = useState("inbox");
  const [searchInput, setSearchInput] = useState("");
  const [searchQ, setSearchQ] = useState("");
  // Page-token history: [current] grows "Older", shrinks "Newer".
  const [tokens, setTokens] = useState<string[]>([""]);
  const pageToken = tokens[tokens.length - 1];
  const [openId, setOpenId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ComposeDraft | null>(null);
  const [showCc, setShowCc] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportSource, setExportSource] = useState<"sent" | "received">("sent");
  const [exportRange, setExportRange] = useState<"complete" | "year" | "custom">("complete");
  const [exportYear, setExportYear] = useState<number>(new Date().getFullYear());
  const [exportFrom, setExportFrom] = useState("");
  const [exportTo, setExportTo] = useState("");
  const listRef = useRef<HTMLDivElement>(null);

  // ---------------------------------------------------------------- accounts
  const accounts = useQuery({
    queryKey: ["email-accounts"],
    queryFn: () => api.emailAccounts(),
  });
  const gmailStatus = useQuery({
    queryKey: ["gmail-status"],
    queryFn: () => api.gmailStatus(),
  });

  // Auto-select the first connected account once the list lands.
  useEffect(() => {
    if (accountId === null && accounts.data && accounts.data.length > 0) {
      const first = accounts.data.find((a) => a.status === "connected") ?? accounts.data[0];
      setAccountId(first.id);
    }
  }, [accounts.data, accountId]);

  const account: EmailAccount | undefined = useMemo(
    () => accounts.data?.find((a) => a.id === accountId),
    [accounts.data, accountId],
  );
  const needsReconnect = account !== undefined && account.status !== "connected";

  // Folder/search resets the token stack and closes the reading pane.
  function resetView() {
    setTokens([""]);
    setOpenId(null);
  }

  // ---------------------------------------------------------------- the list
  const page = useQuery({
    queryKey: ["gmail-messages", accountId, folder, searchQ, pageToken],
    queryFn: () =>
      api.gmailMessages({
        account_id: accountId!,
        folder,
        q: searchQ,
        page_token: pageToken,
      }),
    enabled: accountId !== null,
  });

  const message = useQuery({
    queryKey: ["gmail-message", accountId, openId],
    queryFn: () => api.gmailMessage(accountId!, openId!),
    enabled: accountId !== null && openId !== null,
  });

  function goOlder() {
    const next = page.data?.next_page_token;
    if (next) setTokens([...tokens, next]);
    listRef.current?.scrollTo({ top: 0 });
  }
  function goNewer() {
    if (tokens.length > 1) setTokens(tokens.slice(0, -1));
    listRef.current?.scrollTo({ top: 0 });
  }

  function refresh() {
    queryClient.invalidateQueries({ queryKey: ["gmail-messages"] });
    queryClient.invalidateQueries({ queryKey: ["gmail-message"] });
  }

  const modify = useMutation({
    mutationFn: (input: { message_id: string; add: string[]; remove: string[] }) =>
      api.gmailModify(input.message_id, {
        account_id: accountId!,
        add_labels: input.add,
        remove_labels: input.remove,
      }),
    onSuccess: () => refresh(),
    onError: (e) =>
      setBanner({
        ok: false,
        text: e instanceof ApiError ? e.message : "Gmail refused the change.",
      }),
  });

  const send = useMutation({
    mutationFn: (d: ComposeDraft) =>
      api.gmailSend({
        account_id: accountId!,
        to: d.to,
        cc: d.cc,
        bcc: d.bcc,
        subject: d.subject,
        body: d.body,
        in_reply_to: d.in_reply_to,
        references: d.references,
      }),
    onSuccess: (r) => {
      setDraft(null);
      setBanner({ ok: true, text: `Sent to ${displayAddress(r.to)}.` });
      if (folder === "sent") refresh();
    },
    onError: (e) =>
      setBanner({
        ok: false,
        text: e instanceof ApiError ? e.message : "Gmail refused the send.",
      }),
  });

  const exportXlsx = useMutation({
    mutationFn: () =>
      api.gmailExportAddresses({
        account_id: accountId!,
        source: exportSource,
        year: exportRange === "year" ? exportYear : undefined,
        from_date: exportRange === "custom" ? exportFrom : undefined,
        to_date: exportRange === "custom" ? exportTo : undefined,
      }),
    onSuccess: () =>
      setBanner({ ok: true, text: "Address sheet downloaded." }),
    onError: (e) =>
      setBanner({
        ok: false,
        text: e instanceof ApiError ? e.message : "The export failed — try again.",
      }),
  });

  // ------------------------------------------------------------- compose prep
  function startReply(m: GmailMessage) {
    const h = m.headers;
    const refs = [h.references, h["message-id"]].filter(Boolean).join(" ");
    setDraft({
      to: h.from,
      cc: "", bcc: "",
      subject: h.subject.startsWith("Re:") ? h.subject : `Re: ${h.subject}`,
      body: `\n\n---- On ${h.date || "(unknown date)"}, ${h.from || "sender"} wrote ----\n${m.text || m.snippet}`,
      in_reply_to: h["message-id"],
      references: refs,
      mode: "reply",
    });
    setShowCc(false);
  }

  function startForward(m: GmailMessage) {
    setDraft({
      to: "", cc: "", bcc: "",
      subject: m.headers.subject.startsWith("Fwd:") ? m.headers.subject : `Fwd: ${m.headers.subject}`,
      body: `\n\n---- Forwarded message ----\nFrom: ${m.headers.from}\nDate: ${m.headers.date}\nSubject: ${m.headers.subject}\n\n${m.text || m.snippet}`,
      in_reply_to: "", references: "",
      mode: "forward",
    });
    setShowCc(false);
  }

  // ================================================================= render
  if (accounts.isLoading) {
    return (
      <div className="px-8 py-7 max-w-6xl">
        <PageHeader eyebrow="LeadHunter Pro" title="Email" subtitle="Loading your connected Gmail…" />
      </div>
    );
  }

  // Honest empty state: no account connected yet.
  if (!accounts.data || accounts.data.length === 0 || accountId === null) {
    const configured = gmailStatus.data?.configured === true;
    return (
      <div className="px-8 py-7 max-w-3xl">
        <PageHeader
          eyebrow="LeadHunter Pro"
          title="Email"
          subtitle="Your connected Gmail, inside LeadHunter Pro."
        />
        <div className="mt-6 rounded-xl border border-white/5 bg-white/[0.02] p-6 text-[13px] text-slate-400">
          <Mail className="w-6 h-6 text-indigo-400 mb-3" />
          <p className="text-slate-300 font-medium mb-1">No Gmail connected yet.</p>
          <p className="mb-4">
            Connect your Gmail in Settings to browse, read, reply and export
            addresses from it right here.
          </p>
          {configured ? (
            <button
              onClick={() => {
                window.location.href = googleAuthorizeUrl();
              }}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500"
            >
              + Connect Gmail
            </button>
          ) : (
            <p className="text-amber-300">
              Gmail sign-in isn't configured on the server yet (GOOGLE_CLIENT_ID /
              GOOGLE_CLIENT_SECRET) — see Settings.
            </p>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="px-8 py-7 max-w-[1400px]">
      <PageHeader
        eyebrow="LeadHunter Pro"
        title="Email"
        subtitle="Your connected Gmail — browse, reply, and export addresses."
      />

      {banner && (
        <div
          className={`mt-4 flex items-start justify-between gap-2.5 rounded-lg border px-4 py-3 text-[13px] ${
            banner.ok
              ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
              : "border-rose-500/25 bg-rose-500/10 text-rose-300"
          }`}
        >
          <span>{banner.text}</span>
          <button onClick={() => setBanner(null)} className="text-current/70 hover:text-current">
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {needsReconnect && (
        <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-500/25 bg-amber-500/10 px-4 py-3 text-[13px] text-amber-300">
          <span>
            {account!.email} needs a reconnect —{" "}
            <a className="underline" href={googleAuthorizeUrl()}>connect it again</a>{" "}
            to refresh its access (and grant the newer star/trash permissions).
          </span>
        </div>
      )}

      {/* Account bar + export toggle */}
      <div className="mt-4 flex flex-wrap items-center gap-3">
        {accounts.data.length > 1 ? (
          <div className="relative">
            <select
              value={accountId}
              onChange={(e) => {
                setAccountId(Number(e.target.value));
                resetView();
              }}
              className="appearance-none bg-white/[0.04] border border-white/5 rounded-lg pl-3.5 pr-9 py-2 text-[13px] text-slate-300 outline-none focus:ring-2 focus:ring-indigo-500/40"
            >
              {accounts.data.map((a) => (
                <option key={a.id} value={a.id} className="bg-[#0D1017]">
                  {a.email}
                </option>
              ))}
            </select>
            <ChevronDown className="w-3.5 h-3.5 text-slate-500 absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none" />
          </div>
        ) : (
          <span className="text-[13px] text-slate-400 flex items-center gap-2">
            <Mail className="w-4 h-4 text-indigo-400" />
            {account!.email}
          </span>
        )}

        <button
          onClick={() => setDraft(emptyDraft())}
          className="rounded-lg bg-indigo-600 px-3.5 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500"
        >
          Compose
        </button>

        <button
          onClick={() => setExportOpen((v) => !v)}
          className="ml-auto flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3.5 py-2 text-[13px] text-slate-300 hover:bg-white/[0.06]"
        >
          <FileSpreadsheet className="w-4 h-4 text-emerald-400" />
          Export addresses
        </button>
      </div>

      {/* Export panel — the single-click XLSX of addresses */}
      {exportOpen && (
        <div className="mt-3 rounded-xl border border-white/5 bg-white/[0.02] p-4">
          <div className="flex flex-wrap items-end gap-4">
            <label className="text-[12px] text-slate-400">
              <span className="block mb-1.5">Source</span>
              <select
                value={exportSource}
                onChange={(e) => setExportSource(e.target.value as "sent" | "received")}
                className="bg-white/[0.04] border border-white/5 rounded-lg px-3 py-2 text-[13px] text-slate-300 outline-none"
              >
                <option value="sent" className="bg-[#0D1017]">Sent — everyone I emailed</option>
                <option value="received" className="bg-[#0D1017]">Received — persons only (no services)</option>
              </select>
            </label>

            <label className="text-[12px] text-slate-400">
              <span className="block mb-1.5">Range</span>
              <select
                value={exportRange}
                onChange={(e) => setExportRange(e.target.value as "complete" | "year" | "custom")}
                className="bg-white/[0.04] border border-white/5 rounded-lg px-3 py-2 text-[13px] text-slate-300 outline-none"
              >
                <option value="complete" className="bg-[#0D1017]">Complete</option>
                <option value="year" className="bg-[#0D1017]">Year</option>
                <option value="custom" className="bg-[#0D1017]">Custom dates</option>
              </select>
            </label>

            {exportRange === "year" && (
              <label className="text-[12px] text-slate-400">
                <span className="block mb-1.5">Year</span>
                <select
                  value={exportYear}
                  onChange={(e) => setExportYear(Number(e.target.value))}
                  className="bg-white/[0.04] border border-white/5 rounded-lg px-3 py-2 text-[13px] text-slate-300 outline-none"
                >
                  {YEARS.map((y) => (
                    <option key={y} value={y} className="bg-[#0D1017]">{y}</option>
                  ))}
                </select>
              </label>
            )}

            {exportRange === "custom" && (
              <>
                <label className="text-[12px] text-slate-400">
                  <span className="block mb-1.5">From</span>
                  <input
                    type="date"
                    value={exportFrom}
                    onChange={(e) => setExportFrom(e.target.value)}
                    className="bg-white/[0.04] border border-white/5 rounded-lg px-3 py-2 text-[13px] text-slate-300 outline-none"
                  />
                </label>
                <label className="text-[12px] text-slate-400">
                  <span className="block mb-1.5">To</span>
                  <input
                    type="date"
                    value={exportTo}
                    onChange={(e) => setExportTo(e.target.value)}
                    className="bg-white/[0.04] border border-white/5 rounded-lg px-3 py-2 text-[13px] text-slate-300 outline-none"
                  />
                </label>
              </>
            )}

            <button
              disabled={
                exportXlsx.isPending ||
                (exportRange === "custom" && (!exportFrom || !exportTo))
              }
              onClick={() => exportXlsx.mutate()}
              className="flex items-center gap-2 rounded-lg bg-emerald-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-emerald-500 disabled:opacity-50"
            >
              <Download className="w-4 h-4" />
              {exportXlsx.isPending ? "Exporting…" : "Download XLSX"}
            </button>
          </div>
          {exportSource === "received" && (
            <p className="mt-3 text-[12px] text-slate-500">
              Received export keeps only senders that look like real persons —
              services like Facebook/Instagram notifications and role addresses
              (info@, noreply@…) are filtered out.
            </p>
          )}
        </div>
      )}

      {/* Folder tabs + search */}
      <div className="mt-4 flex flex-wrap items-center gap-2">
        {FOLDERS.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            onClick={() => {
              setFolder(key);
              resetView();
            }}
            className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] transition-colors ${
              folder === key
                ? "bg-indigo-500/15 text-white"
                : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200"
            }`}
          >
            <Icon className="w-3.5 h-3.5" strokeWidth={1.75} />
            {label}
          </button>
        ))}

        <form
          className="ml-auto flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setSearchQ(searchInput.trim());
            resetView();
          }}
        >
          <div className="relative">
            <Search className="w-3.5 h-3.5 text-slate-500 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
            <input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search — Gmail syntax (from:, has:attachment…)"
              className="w-72 bg-white/[0.04] border border-white/5 rounded-lg pl-9 pr-3 py-2 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40"
            />
          </div>
        </form>
      </div>

      {/* List + reading pane */}
      <div className="mt-4 grid grid-cols-1 lg:grid-cols-[380px_1fr] gap-4">
        <div className="rounded-xl border border-white/5 bg-white/[0.02] overflow-hidden flex flex-col max-h-[calc(100vh-320px)]">
          <div ref={listRef} className="overflow-y-auto flex-1 divide-y divide-white/5">
            {page.isLoading && (
              <p className="p-4 text-[13px] text-slate-500">Loading messages…</p>
            )}
            {page.isError && (
              <p className="p-4 text-[13px] text-rose-300">
                {page.error instanceof ApiError ? page.error.message : "Gmail could not be reached."}
              </p>
            )}
            {page.data && page.data.messages.length === 0 && (
              <p className="p-4 text-[13px] text-slate-500">
                Nothing here{searchQ ? ` for “${searchQ}”` : ""}.
              </p>
            )}
            {page.data?.messages.map((m) => (
              <button
                key={m.id}
                onClick={() => setOpenId(m.id)}
                className={`w-full text-left px-4 py-3 transition-colors ${
                  openId === m.id ? "bg-indigo-500/10" : "hover:bg-white/[0.03]"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.stopPropagation();
                      modify.mutate({
                        message_id: m.id,
                        add: m.starred ? [] : ["STARRED"],
                        remove: m.starred ? ["STARRED"] : [],
                      });
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") e.currentTarget.click();
                    }}
                    className="shrink-0"
                    title={m.starred ? "Remove star" : "Add star"}
                  >
                    <Star
                      className={`w-3.5 h-3.5 ${
                        m.starred ? "text-amber-400 fill-amber-400" : "text-slate-600"
                      }`}
                    />
                  </span>
                  <span
                    className={`truncate text-[13px] ${
                      m.unread ? "text-white font-semibold" : "text-slate-300"
                    }`}
                  >
                    {folder === "sent" ? `To: ${displayAddress(m.to)}` : displayAddress(m.from)}
                  </span>
                  <span className="ml-auto shrink-0 text-[11px] text-slate-500">
                    {fmtDate(m.date)}
                  </span>
                </div>
                <div className={`mt-0.5 truncate text-[12.5px] ${m.unread ? "text-slate-200 font-medium" : "text-slate-400"}`}>
                  {m.subject || "(no subject)"}
                </div>
                <div className="mt-0.5 truncate text-[12px] text-slate-500">{m.snippet}</div>
              </button>
            ))}
          </div>

          {/* Token pagination */}
          <div className="flex items-center justify-between border-t border-white/5 px-4 py-2 text-[12px] text-slate-500">
            <button
              onClick={goNewer}
              disabled={tokens.length <= 1}
              className="flex items-center gap-1 rounded px-2 py-1 hover:bg-white/[0.05] disabled:opacity-30"
            >
              <ChevronLeft className="w-3.5 h-3.5" /> Newer
            </button>
            {page.data ? `${page.data.total_estimate} messages` : ""}
            <button
              onClick={goOlder}
              disabled={!page.data?.next_page_token}
              className="flex items-center gap-1 rounded px-2 py-1 hover:bg-white/[0.05] disabled:opacity-30"
            >
              Older <ChevronRight className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        {/* Reading pane */}
        <div className="rounded-xl border border-white/5 bg-white/[0.02] overflow-y-auto max-h-[calc(100vh-320px)]">
          {!openId && (
            <div className="h-full flex flex-col items-center justify-center text-slate-500 p-8">
              <MailOpen className="w-8 h-8 mb-3 opacity-50" />
              <p className="text-[13px]">Select a message to read it.</p>
            </div>
          )}
          {openId && message.isLoading && (
            <p className="p-4 text-[13px] text-slate-500">Loading message…</p>
          )}
          {openId && message.isError && (
            <p className="p-4 text-[13px] text-rose-300">
              {message.error instanceof ApiError ? message.error.message : "The message could not be read."}
            </p>
          )}
          {message.data && (
            <div className="p-5">
              {/* Header block */}
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <h2 className="text-[15px] font-semibold text-white break-words">
                    {message.data.headers.subject || "(no subject)"}
                  </h2>
                  <p className="mt-1 text-[12.5px] text-slate-400">
                    <span className="text-slate-500">From </span>
                    {message.data.headers.from || "(unknown)"}
                  </p>
                  <p className="text-[12.5px] text-slate-400">
                    <span className="text-slate-500">To </span>
                    {message.data.headers.to || "(unknown)"}
                    {message.data.headers.cc && (
                      <><span className="text-slate-500"> · Cc </span>{message.data.headers.cc}</>
                    )}
                  </p>
                  <p className="mt-0.5 text-[11.5px] text-slate-500">
                    {fmtDate(message.data.headers.date)}
                  </p>
                </div>
              </div>

              {/* Actions */}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  onClick={() => startReply(message.data!)}
                  className="flex items-center gap-1.5 rounded-lg bg-indigo-600 px-3 py-1.5 text-[12.5px] font-semibold text-white hover:bg-indigo-500"
                >
                  <Reply className="w-3.5 h-3.5" /> Reply
                </button>
                <button
                  onClick={() => startForward(message.data!)}
                  className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.06]"
                >
                  <Forward className="w-3.5 h-3.5" /> Forward
                </button>
                <button
                  onClick={() =>
                    modify.mutate({
                      message_id: message.data!.id,
                      add: message.data!.starred ? [] : ["STARRED"],
                      remove: message.data!.starred ? ["STARRED"] : [],
                    })
                  }
                  className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.06]"
                >
                  <Star
                    className={`w-3.5 h-3.5 ${message.data.starred ? "text-amber-400 fill-amber-400" : ""}`}
                  />
                  {message.data.starred ? "Unstar" : "Star"}
                </button>
                <button
                  onClick={() =>
                    modify.mutate({
                      message_id: message.data!.id,
                      add: ["UNREAD"],
                      remove: [],
                    })
                  }
                  className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.06]"
                >
                  <MailOpen className="w-3.5 h-3.5" /> Mark unread
                </button>
                <button
                  onClick={() =>
                    modify.mutate({
                      message_id: message.data!.id,
                      add: [],
                      remove: ["INBOX"],
                    })
                  }
                  className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.06]"
                >
                  <Archive className="w-3.5 h-3.5" /> Archive
                </button>
                <button
                  onClick={() =>
                    modify.mutate({
                      message_id: message.data!.id,
                      add: ["TRASH"],
                      remove: ["INBOX"],
                    })
                  }
                  className="flex items-center gap-1.5 rounded-lg border border-rose-500/25 bg-rose-500/10 px-3 py-1.5 text-[12.5px] text-rose-300 hover:bg-rose-500/20"
                >
                  <Trash2 className="w-3.5 h-3.5" /> Trash
                </button>
              </div>

              {/* Body — the HTML part in a sandboxed iframe (scripts can never
                  run: sandbox with no allow-* permissions), else plain text. */}
              <div className="mt-4">
                {message.data.html ? (
                  <iframe
                    title="Message body"
                    sandbox=""
                    srcDoc={`<!doctype html><meta charset="utf-8"><style>body{font:13px/1.6 system-ui,sans-serif;color:#222;margin:0;padding:2px;word-wrap:break-word}img{max-width:100%}a{color:#4f46e5}</style>${message.data.html}`}
                    className="w-full min-h-[200px] rounded-lg border border-white/5 bg-white"
                  />
                ) : (
                  <pre className="whitespace-pre-wrap text-[13px] text-slate-300 font-sans">
                    {message.data.text || "(empty message)"}
                  </pre>
                )}
              </div>

              {/* Attachments */}
              {message.data.attachments.length > 0 && (
                <div className="mt-4 border-t border-white/5 pt-3">
                  <p className="mb-2 text-[11.5px] uppercase tracking-wide text-slate-500">
                    {message.data.attachments.length} attachment{message.data.attachments.length > 1 ? "s" : ""}
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {message.data.attachments.map((a) => (
                      <button
                        key={a.attachment_id}
                        onClick={() =>
                          api
                            .gmailAttachment(accountId!, message.data!.id, a.attachment_id, a.filename)
                            .catch((e) =>
                              setBanner({
                                ok: false,
                                text: e instanceof ApiError ? e.message : "Attachment download failed.",
                              }),
                            )
                        }
                        className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-[12.5px] text-slate-300 hover:bg-white/[0.06]"
                        title={`Download ${a.filename} (${fmtSize(a.size)})`}
                      >
                        <Paperclip className="w-3.5 h-3.5 text-indigo-400" />
                        <span className="max-w-[220px] truncate">{a.filename}</span>
                        <span className="text-slate-500">{fmtSize(a.size)}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Compose / reply / forward modal */}
      {draft && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="w-full max-w-2xl rounded-xl border border-white/10 bg-[#0D1017] p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-[14px] font-semibold text-white">
                {draft.mode === "reply" ? "Reply" : draft.mode === "forward" ? "Forward" : "New email"}
              </h3>
              <button onClick={() => setDraft(null)} className="text-slate-500 hover:text-white">
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="space-y-2.5">
              <input
                value={draft.to}
                onChange={(e) => setDraft({ ...draft, to: e.target.value })}
                placeholder="To"
                className={INPUT}
              />
              {showCc && (
                <>
                  <input
                    value={draft.cc}
                    onChange={(e) => setDraft({ ...draft, cc: e.target.value })}
                    placeholder="Cc"
                    className={INPUT}
                  />
                  <input
                    value={draft.bcc}
                    onChange={(e) => setDraft({ ...draft, bcc: e.target.value })}
                    placeholder="Bcc"
                    className={INPUT}
                  />
                </>
              )}
              {!showCc && (
                <button
                  onClick={() => setShowCc(true)}
                  className="text-[12px] text-indigo-400 hover:text-indigo-300"
                >
                  + Cc / Bcc
                </button>
              )}
              <input
                value={draft.subject}
                onChange={(e) => setDraft({ ...draft, subject: e.target.value })}
                placeholder="Subject"
                className={INPUT}
              />
              <textarea
                value={draft.body}
                onChange={(e) => setDraft({ ...draft, body: e.target.value })}
                placeholder="Write your message…"
                rows={10}
                className={`${INPUT} resize-y`}
              />
            </div>

            {send.isError && (
              <p className="mt-3 text-[12.5px] text-rose-300">
                {send.error instanceof ApiError ? send.error.message : "The send failed."}
              </p>
            )}

            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setDraft(null)}
                className="rounded-lg border border-white/10 px-4 py-2 text-[13px] text-slate-300 hover:bg-white/[0.05]"
              >
                Discard
              </button>
              <button
                onClick={() => send.mutate(draft)}
                disabled={send.isPending || draft.to.trim().length < 3 || !draft.subject.trim()}
                className="flex items-center gap-2 rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
              >
                <Send className="w-4 h-4" />
                {send.isPending ? "Sending…" : "Send"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
