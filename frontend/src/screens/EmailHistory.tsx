import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, ChevronRight, Search, X } from "lucide-react";
import { ApiError, api } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import type { Campaign, CampaignHistoryRow } from "../types";

type View = "sent" | "replied" | "bounced" | "followup";
const VIEWS: { key: View; label: string; detail: string }[] = [
  { key: "sent", label: "Sent", detail: "First emails" },
  { key: "replied", label: "Replied", detail: "Replies received" },
  { key: "bounced", label: "Bounced", detail: "Delivery failures" },
  { key: "followup", label: "Follow-ups", detail: "Later emails" },
];
const PAGE_SIZE = 50;
const INPUT = "mt-1 min-h-11 w-full rounded-lg border border-white/10 bg-white/[0.04] px-3 text-[13px] text-slate-100 outline-none focus:ring-2 focus:ring-indigo-400/50";
const BUTTON = "min-h-11 rounded-lg border border-white/10 px-3 text-xs font-medium text-slate-200 hover:bg-white/5 disabled:opacity-40";

function formatDate(value: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : `${parsed.toLocaleString("en-PK", {
    timeZone: "Asia/Karachi", year: "numeric", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", hour12: true,
  })} PKT`;
}

function usesAccount(campaign: Campaign, accountId: number): boolean {
  return accountId === 0 || campaign.account_id === accountId ||
    (campaign.account_ids ?? []).includes(accountId);
}

export default function EmailHistory() {
  const [view, setView] = useState<View>("sent");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [accountId, setAccountId] = useState(0);
  const [campaignId, setCampaignId] = useState(0);
  const [emailDraft, setEmailDraft] = useState("");
  const [email, setEmail] = useState("");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<CampaignHistoryRow | null>(null);
  const accounts = useQuery({ queryKey: ["email-accounts"], queryFn: () => api.emailAccounts() });
  const campaigns = useQuery({ queryKey: ["campaigns"], queryFn: () => api.campaigns() });
  const availableCampaigns = (campaigns.data ?? []).filter((campaign) => usesAccount(campaign, accountId));
  const history = useQuery({
    queryKey: ["campaign-history", view, fromDate, toDate, accountId, campaignId, email, page],
    queryFn: () => api.exploreCampaignActivity({
      view, from_date: fromDate, to_date: toDate, account_id: accountId,
      campaign_id: campaignId, email, offset: page * PAGE_SIZE, limit: PAGE_SIZE,
    }),
    refetchInterval: 30_000,
  });
  const result = history.data;
  const totalPages = Math.max(1, Math.ceil((result?.total ?? 0) / PAGE_SIZE));
  const filtered = !!(fromDate || toDate || accountId || campaignId || email);
  const resetFilters = () => {
    setFromDate(""); setToDate(""); setAccountId(0); setCampaignId(0);
    setEmailDraft(""); setEmail(""); setPage(0);
  };
  return <div className="workspace-page page-focused email-history">
    <PageHeader eyebrow="Engage" title="Email History"
      subtitle="Find every recorded send, reply, bounce and follow-up by Pakistan date, campaign or Gmail account." />
    <section aria-label="Email history filters" className="mt-5 rounded-xl border border-white/10 bg-white/[0.02] p-4 sm:p-5">
      <div role="tablist" aria-label="Email activity" className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {VIEWS.map((tab) => <button key={tab.key} type="button" role="tab" aria-selected={view === tab.key}
          onClick={() => { setView(tab.key); setPage(0); setSelected(null); }}
          className={`min-h-14 rounded-lg border px-3 py-2 text-left ${view === tab.key ? "border-indigo-400 bg-indigo-500/15 text-white" : "border-white/10 text-slate-300 hover:bg-white/5"}`}>
          <span className="block text-[13px] font-semibold">{tab.label}</span>
          <span className="block text-[11px] text-slate-400">{tab.detail}</span>
        </button>)}
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label className="text-xs text-slate-300">From date
          <input type="date" value={fromDate} max={toDate || undefined} onChange={(e) => { setFromDate(e.target.value); setPage(0); }} className={INPUT} />
        </label>
        <label className="text-xs text-slate-300">To date
          <input type="date" value={toDate} min={fromDate || undefined} onChange={(e) => { setToDate(e.target.value); setPage(0); }} className={INPUT} />
        </label>
        <label className="text-xs text-slate-300">Gmail account
          <select value={accountId} onChange={(e) => {
            const nextAccountId = Number(e.target.value);
            setAccountId(nextAccountId);
            setCampaignId((current) => current && !campaigns.data?.some((campaign) =>
              campaign.id === current && usesAccount(campaign, nextAccountId)) ? 0 : current);
            setPage(0);
          }} className={INPUT}>
            <option value={0}>All accounts</option>
            {(accounts.data ?? []).map((a) => <option key={a.id} value={a.id}>{a.email}</option>)}
          </select>
        </label>
        <label className="text-xs text-slate-300">Campaign
          <select value={campaignId} onChange={(e) => { setCampaignId(Number(e.target.value)); setPage(0); }} className={INPUT}>
            <option value={0}>{accountId ? "All campaigns for this account" : "All campaigns"}</option>
            {availableCampaigns.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>
      </div>
      <form className="mt-3 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); setEmail(emailDraft.trim()); setPage(0); }}>
        <label className="min-w-[200px] flex-1 text-xs text-slate-300">Recipient email
          <input type="search" value={emailDraft} onChange={(e) => setEmailDraft(e.target.value)} placeholder="Search address…" className={INPUT} />
        </label>
        <button type="submit" className={`${BUTTON} inline-flex items-center gap-2`}><Search className="h-4 w-4" /> Search</button>
        {filtered && <button type="button" onClick={resetFilters} className={BUTTON}>Clear filters</button>}
      </form>
    </section>

    <section aria-label="Email history results" className="mt-4 rounded-xl border border-white/10 bg-white/[0.02] p-4 sm:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div><h2 className="text-base font-semibold text-white">{VIEWS.find((v) => v.key === view)?.label} history</h2>
          <p className="mt-1 text-xs text-slate-400">
            {history.isLoading ? "Loading records…" : `${(result?.total ?? 0).toLocaleString()} records`}
            {result?.first_date && !fromDate && ` · from ${result.first_date}`}
            {result && ` · page ${page + 1} of ${totalPages}`}
          </p>
        </div>
        <button type="button" onClick={() => void history.refetch()} disabled={history.isFetching} className={BUTTON}>Refresh</button>
      </div>
      {history.isError && <p role="alert" className="mt-4 text-sm text-rose-300">Could not load history. Try Refresh.</p>}
      {result && result.rows.length === 0 && <p className="mt-4 text-sm text-slate-400">No matching records. Change the filters or activity tab.</p>}
      {result && result.rows.length > 0 && <div className="mt-4 space-y-2 md:hidden">
        {result.rows.map((row) => <div key={`mobile-${view}-${row.campaign_id}-${row.email}-${row.send_id}`} className="rounded-lg border border-white/10 p-3 text-xs">
          <p className="break-all font-semibold text-white">{row.email}</p>
          <p className="mt-1 text-slate-300">{row.campaign_name}{row.step > 0 && ` · Follow-up ${row.step}`}</p>
          <p className="mt-1 break-all text-slate-400">{row.account_email || `Account #${row.account_id}`}</p>
          <p className="mt-1 text-slate-400">{formatDate(row.event_at)}</p>
          <button type="button" onClick={() => setSelected(row)}
            className="mt-2 min-h-11 rounded-lg border border-indigo-400/30 px-3 text-indigo-100">View record</button>
        </div>)}
      </div>}
      {result && result.rows.length > 0 && <div className="mt-4 hidden overflow-x-auto rounded-lg border border-white/10 md:block">
        <table className="w-full min-w-[700px] text-left text-xs">
          <thead className="bg-white/[0.04] text-slate-300"><tr>
            <th className="px-3 py-3 font-medium">Recipient</th><th className="px-3 py-3 font-medium">Campaign</th>
            <th className="px-3 py-3 font-medium">Gmail account</th><th className="px-3 py-3 font-medium">Date</th>
            <th className="px-3 py-3 font-medium">Record</th>
          </tr></thead>
          <tbody>{result.rows.map((row) => <tr key={`${view}-${row.campaign_id}-${row.email}-${row.send_id}`} className="border-t border-white/5 align-top">
            <td className="break-all px-3 py-3 text-slate-100">{row.email}{row.step > 0 && <span className="block text-[11px] text-slate-400">Follow-up {row.step}</span>}</td>
            <td className="px-3 py-3 text-slate-300">{row.campaign_name}</td>
            <td className="break-all px-3 py-3 text-slate-300">{row.account_email || `Account #${row.account_id}`}</td>
            <td className="whitespace-nowrap px-3 py-3 text-slate-300">{formatDate(row.event_at)}</td>
            <td className="px-3 py-2"><button type="button" onClick={() => setSelected(row)}
              className="min-h-10 rounded-lg px-2 text-indigo-200 underline hover:text-white">View record</button></td>
          </tr>)}</tbody>
        </table>
      </div>}
      {result && result.total > PAGE_SIZE && <div className="mt-3 flex items-center justify-between gap-3">
        <button type="button" disabled={page === 0} onClick={() => setPage((p) => p - 1)} className={`${BUTTON} inline-flex items-center gap-1`}><ChevronLeft className="h-4 w-4" /> Newer</button>
        <span className="text-xs text-slate-400">{page + 1} / {totalPages}</span>
        <button type="button" disabled={page + 1 >= totalPages} onClick={() => setPage((p) => p + 1)} className={`${BUTTON} inline-flex items-center gap-1`}>Older <ChevronRight className="h-4 w-4" /></button>
      </div>}
    </section>
    {result && <div className="mt-4 grid gap-4 lg:grid-cols-2">
      <section aria-label="Activity by date" className="rounded-xl border border-white/10 bg-white/[0.02] p-4">
        <h2 className="text-sm font-semibold text-white">Date-wise total</h2>
        <p className="mt-1 text-xs text-slate-400">Choose any date, including the first campaign day.</p>
        <div className="mt-3 max-h-60 overflow-y-auto">{result.by_date.map((item) => <button key={item.date} type="button"
          onClick={() => { setFromDate(item.date); setToDate(item.date); setPage(0); }}
          className="flex min-h-10 w-full items-center justify-between border-t border-white/5 px-1 text-left text-xs text-slate-200 hover:text-indigo-200">
          <span>{item.date}</span><strong>{item.count.toLocaleString()}</strong>
        </button>)}</div>
      </section>
      <section aria-label="Activity by Gmail account" className="rounded-xl border border-white/10 bg-white/[0.02] p-4">
        <h2 className="text-sm font-semibold text-white">Gmail account-wise total</h2>
        <p className="mt-1 text-xs text-slate-400">Counts follow the chosen dates and campaign.</p>
        <div className="mt-3 max-h-60 overflow-y-auto">{result.by_account.map((item) => <button key={item.account_id} type="button"
          onClick={() => { setAccountId(item.account_id); setPage(0); }}
          className="flex min-h-10 w-full items-center justify-between gap-3 border-t border-white/5 px-1 text-left text-xs text-slate-200 hover:text-indigo-200">
          <span className="break-all">{item.account_email || `Account #${item.account_id}`}</span><strong>{item.count.toLocaleString()}</strong>
        </button>)}</div>
      </section>
    </div>}
    {selected && <RecipientRecord row={selected} onClose={() => setSelected(null)} />}
  </div>;
}

function RecipientRecord({ row, onClose }: { row: CampaignHistoryRow; onClose: () => void }) {
  const timeline = useQuery({ queryKey: ["campaign-recipient-timeline", row.campaign_id, row.email, row.send_id], queryFn: () => api.campaignRecipientTimeline(row.send_id, row.campaign_id, row.email) });
  const [readReply, setReadReply] = useState(false);
  const reply = useQuery({ queryKey: ["campaign-reply", row.send_id], queryFn: () => api.campaignReplyContent(row.send_id), enabled: readReply, retry: false });
  return <div role="dialog" aria-modal="true" aria-label="Email record" className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-3 sm:p-5" onClick={onClose}>
    <div className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-xl border border-white/10 bg-[#141821] p-4 shadow-2xl sm:p-6" onClick={(e) => e.stopPropagation()}>
      <div className="flex items-start justify-between gap-3"><div><h2 className="text-base font-semibold text-white">Email record</h2><p className="mt-1 break-all text-xs text-slate-300">{row.email}</p></div>
        <button type="button" onClick={onClose} aria-label="Close email record" className="flex min-h-11 min-w-11 items-center justify-center rounded-lg text-slate-300 hover:bg-white/10"><X className="h-4 w-4" /></button>
      </div>
      <p className="mt-3 text-xs text-slate-400">Campaign: <span className="text-slate-200">{row.campaign_name}</span></p>
      <p className="mt-1 break-all text-xs text-slate-400">From: <span className="text-slate-200">{row.account_email || `Account #${row.account_id}`}</span> · {formatDate(row.event_at)}</p>
      {timeline.isLoading && <p className="mt-4 text-sm text-slate-400">Loading full record…</p>}
      {timeline.isError && <p role="alert" className="mt-4 text-sm text-rose-300">Could not load this record.</p>}
      {timeline.data && <>
        <div className="mt-4 space-y-2">{timeline.data.sends.map((send) => <div key={send.send_id} className="rounded-lg border border-white/10 p-3">
          <div className="flex flex-wrap justify-between gap-2 text-xs"><strong className="text-slate-100">{send.step === 0 ? "First email" : `Follow-up ${send.step}`}</strong><span className="capitalize text-slate-300">{send.state}</span></div>
          <p className="mt-1 break-all text-xs text-slate-300">Subject: {send.subject || "Not sent yet"}</p>
          {send.body && <div className="mt-2 max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg border border-white/10 bg-black/20 p-3 text-sm text-slate-200">{send.body}</div>}
          <p className="mt-1 text-xs text-slate-400">From: {send.account_email || `Account #${send.account_id}`}</p>
          <p className="mt-1 text-xs text-slate-400">{send.sent_at ? `Sent: ${formatDate(send.sent_at)}` : send.not_before ? `Scheduled after: ${formatDate(send.not_before)}` : "Awaiting send"}</p>
          {send.error && <p className="mt-1 text-xs text-rose-300">{send.error}</p>}
        </div>)}</div>
        {timeline.data.reply && <div className="mt-3 rounded-lg border border-amber-400/25 bg-amber-500/5 p-3 text-xs text-slate-200">
          <strong className="text-amber-200">Reply received</strong> · {formatDate(timeline.data.reply.received_at)}
          <p className="mt-1">Subject: {timeline.data.reply.subject || "(No subject)"}</p>
          <button type="button" onClick={() => setReadReply(true)} className="mt-2 min-h-10 text-indigo-200 underline hover:text-white">Read reply from Gmail</button>
          {reply.isLoading && <p className="mt-2 text-slate-400">Reading…</p>}
          {reply.isError && <p role="alert" className="mt-2 text-rose-300">{reply.error instanceof ApiError ? reply.error.message : "Could not load reply."}</p>}
          {reply.data && <div className="mt-2 max-h-56 overflow-y-auto whitespace-pre-wrap rounded-lg border border-white/10 p-3 text-sm">{reply.data.found ? reply.data.text || "No plain-text content." : "Original reply is no longer available in Gmail."}</div>}
        </div>}
        {timeline.data.bounce && <div className="mt-3 rounded-lg border border-rose-400/25 bg-rose-500/5 p-3 text-xs text-rose-200">
          <strong>Bounced</strong> · {formatDate(timeline.data.bounce.bounced_at)}
          {timeline.data.bounce.reason && <p className="mt-1 break-words">{timeline.data.bounce.reason}</p>}
        </div>}
      </>}
    </div>
  </div>;
}
