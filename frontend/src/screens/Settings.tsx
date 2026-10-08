import { useEffect, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, ShieldCheck, Info, Mail, Send, Trash2, CheckCircle2, AlertTriangle } from "lucide-react";
import { ApiError, api, getStoredApiKey, googleAuthorizeUrl, setStoredApiKey } from "../api/client";
import { PageHeader } from "../components/PageHeader";
import { ConnectionStatus } from "../components/Topbar";

/** The OAuth return banner: ?gmail=connected:<email> | ?gmail=error:<code>. */
function readGmailBanner(): { ok: boolean; text: string } | null {
  const m = /[?&]gmail=([^&]+)/.exec(window.location.search);
  if (!m) return null;
  // One-shot: show it, then take it out of the URL so a refresh doesn't re-show it.
  window.history.replaceState({}, "", window.location.pathname);
  const value = decodeURIComponent(m[1]);
  if (value.startsWith("connected:")) {
    return { ok: true, text: `Gmail connected: ${value.slice("connected:".length)}` };
  }
  const reason = value.startsWith("error:") ? value.slice("error:".length) : value;
  const friendly: Record<string, string> = {
    access_denied: "Google permission denied — you cancelled the consent screen.",
    "expired-state": "The connect window expired (10 min). Please try again.",
    "missing-code": "Google did not return an authorization code. Please try again.",
    "exchange-failed": "Google rejected the authorization code. Please try again.",
    "no-email-claim": "Google did not tell us which Gmail was selected. Please try again.",
  };
  return { ok: false, text: friendly[reason] || `Gmail connect failed (${reason}).` };
}

export default function Settings() {
  const [apiKey, setApiKey] = useState(getStoredApiKey());
  const [showKey, setShowKey] = useState(false);
  const [saved, setSaved] = useState(false);
  const [banner, setBanner] = useState<{ ok: boolean; text: string } | null>(null);
  const [showSmtpForm, setShowSmtpForm] = useState(false);
  const [smtpForm, setSmtpForm] = useState({
    email: "", host: "", port: "465", security: "ssl" as "ssl" | "starttls",
    username: "", password: "",
  });
  const queryClient = useQueryClient();

  useEffect(() => {
    setBanner(readGmailBanner());
  }, []);

  function save() {
    setStoredApiKey(apiKey.trim());
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  // ---- Email accounts (Phase E2: Gmail via Google OAuth) -------------------
  const accounts = useQuery({
    queryKey: ["email-accounts"],
    queryFn: () => api.emailAccounts(),
  });
  const gmailStatus = useQuery({
    queryKey: ["gmail-status"],
    queryFn: () => api.gmailStatus(),
  });

  const testSend = useMutation({
    mutationFn: (id: number) => api.sendTestEmail(id),
    onSuccess: (r) => setBanner({ ok: true, text: `Test email sent to ${r.to} — check that inbox.` }),
    onError: (e) =>
      setBanner({
        ok: false,
        text: e instanceof ApiError ? e.message : "Test send failed — is the backend reachable?",
      }),
  });

  const disconnect = useMutation({
    mutationFn: (id: number) => api.disconnectEmailAccount(id),
    onSuccess: () => {
      setBanner({ ok: true, text: "Email account disconnected." });
      queryClient.invalidateQueries({ queryKey: ["email-accounts"] });
    },
  });

  const connectSmtp = useMutation({
    mutationFn: () => api.connectSmtpAccount({
      email: smtpForm.email.trim(),
      host: smtpForm.host.trim(),
      port: Number(smtpForm.port),
      security: smtpForm.security,
      username: smtpForm.username.trim(),
      password: smtpForm.password,
    }),
    onSuccess: (account) => {
      setBanner({ ok: true, text: `SMTP connected: ${account.email}` });
      setSmtpForm({ email: "", host: "", port: "465", security: "ssl", username: "", password: "" });
      setShowSmtpForm(false);
      queryClient.invalidateQueries({ queryKey: ["email-accounts"] });
    },
    onError: (error) => setBanner({
      ok: false,
      text: error instanceof ApiError ? error.message : "SMTP connection failed. Check the server details and try again.",
    }),
  });

  function submitSmtp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!connectSmtp.isPending) connectSmtp.mutate();
  }

  const configured = gmailStatus.data?.configured === true;

  return (
    <div className="workspace-page page-settings">
      <PageHeader
        eyebrow="LeadHunter Pro"
        title="Settings"
        subtitle="API access, email sending, and connection details."
      />
      {(accounts.isError || gmailStatus.isError || disconnect.isError) && <div role="alert" className="mt-4 rounded-lg bg-rose-500/10 border border-rose-400/20 p-4 text-sm text-rose-200">{disconnect.isError ? "Could not disconnect this account. Please try again." : "Account connection details could not be loaded."}<button className="ml-2 underline" onClick={() => { void accounts.refetch(); void gmailStatus.refetch(); }}>Refresh</button></div>}

      {banner && (
        <div
          className={`mt-4 flex items-start gap-2.5 rounded-lg border px-4 py-3 text-[13px] ${
            banner.ok
              ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
              : "border-rose-500/25 bg-rose-500/10 text-rose-300"
          }`}
        >
          {banner.ok ? (
            <CheckCircle2 className="w-4 h-4 shrink-0 mt-0.5" />
          ) : (
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
          )}
          <p className="flex-1">{banner.text}</p>
          <button onClick={() => setBanner(null)} className="text-slate-400 hover:text-slate-200">
            ✕
          </button>
        </div>
      )}

      {/* Email accounts (sending) */}
      <div className="mt-6 ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <Mail className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">Email accounts</h2>
            <p className="text-[12.5px] text-slate-500">
              Connect a sending account with Gmail or secure SMTP.
            </p>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          {configured && (
            <button
              type="button"
              onClick={() => { window.location.href = googleAuthorizeUrl(); }}
              className="min-h-11 rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500"
            >
              + Connect Gmail
            </button>
          )}
          <button
            type="button"
            aria-expanded={showSmtpForm}
            aria-controls="smtp-connect-form"
            onClick={() => {
              setShowSmtpForm((open) => !open);
              if (showSmtpForm) setSmtpForm((f) => ({ ...f, password: "" }));
            }}
            className="min-h-11 rounded-lg border border-white/15 px-4 py-2 text-[13px] font-semibold text-slate-200 hover:bg-white/[0.06]"
          >
            {showSmtpForm ? "Hide SMTP form" : "+ Connect SMTP"}
          </button>
        </div>
        {gmailStatus.isPending && <p role="status" className="mt-3 text-sm text-slate-400">Checking Gmail availability…</p>}
        {!gmailStatus.isPending && !gmailStatus.isError && !configured && (
          <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-[12.5px] text-amber-300">
            <Info className="w-4 h-4 shrink-0 mt-0.5" />
            <p>Gmail connection is unavailable. You can still connect a secure SMTP account.</p>
          </div>
        )}

        {showSmtpForm && (
          <form id="smtp-connect-form" onSubmit={submitSmtp} autoComplete="off" className="mt-4 rounded-xl border border-white/10 bg-white/[0.03] p-4">
            <h3 className="text-sm font-semibold text-slate-100">Secure SMTP connection</h3>
            <p className="mt-1 text-xs text-slate-400">Connection is verified before saving. Use an app password if your mail provider requires one.</p>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <label className="text-xs font-medium text-slate-300">Sending email
                <input type="email" required value={smtpForm.email} onChange={(e) => setSmtpForm((f) => ({ ...f, email: e.target.value }))} placeholder="you@company.com" className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950/60 px-3 py-2.5 text-sm text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/50" />
              </label>
              <label className="text-xs font-medium text-slate-300">SMTP host
                <input required value={smtpForm.host} onChange={(e) => setSmtpForm((f) => ({ ...f, host: e.target.value }))} placeholder="smtp.company.com" className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950/60 px-3 py-2.5 text-sm text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/50" />
              </label>
              <label className="text-xs font-medium text-slate-300">Security
                <select value={smtpForm.security} onChange={(e) => setSmtpForm((f) => ({ ...f, security: e.target.value as "ssl" | "starttls", port: e.target.value === "ssl" ? "465" : "587" }))} className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950 px-3 py-2.5 text-sm text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/50">
                  <option value="ssl">SSL / TLS</option>
                  <option value="starttls">STARTTLS</option>
                </select>
              </label>
              <label className="text-xs font-medium text-slate-300">Port
                <input type="number" min="1" max="65535" required value={smtpForm.port} onChange={(e) => setSmtpForm((f) => ({ ...f, port: e.target.value }))} className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950/60 px-3 py-2.5 text-sm text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/50" />
              </label>
              <label className="text-xs font-medium text-slate-300">SMTP username
                <input required value={smtpForm.username} onChange={(e) => setSmtpForm((f) => ({ ...f, username: e.target.value }))} placeholder="Usually your email address" className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950/60 px-3 py-2.5 text-sm text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/50" />
              </label>
              <label className="text-xs font-medium text-slate-300">Password or app password
                <input type="password" required value={smtpForm.password} onChange={(e) => setSmtpForm((f) => ({ ...f, password: e.target.value }))} className="mt-1 w-full rounded-lg border border-white/10 bg-slate-950/60 px-3 py-2.5 text-sm text-slate-100 outline-none focus:ring-2 focus:ring-indigo-500/50" />
              </label>
            </div>
            <p className="mt-3 text-xs text-slate-400">SMTP sends mail only. Inbox and automatic reply detection require a Gmail connection.</p>
            <div className="mt-4 flex flex-wrap gap-2">
              <button type="submit" disabled={connectSmtp.isPending} className="min-h-11 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white hover:bg-indigo-500 disabled:opacity-50">
                {connectSmtp.isPending ? "Checking connection…" : "Verify and connect"}
              </button>
              <button type="button" onClick={() => { setShowSmtpForm(false); setSmtpForm((f) => ({ ...f, password: "" })); }} className="min-h-11 rounded-lg border border-white/10 px-4 py-2 text-sm text-slate-300 hover:bg-white/[0.05]">Cancel</button>
            </div>
          </form>
        )}

        {accounts.isLoading && <p className="mt-4 text-[13px] text-slate-400">Loading accounts…</p>}
        {accounts.data && accounts.data.length > 0 && (
          <ul className="mt-4 divide-y divide-white/5">
            {accounts.data.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center gap-3 py-3">
                <div className="min-w-[12rem] flex-1">
                  <p className="break-all text-[13.5px] text-slate-200">{a.email}</p>
                  <p className="text-xs text-slate-400">{a.provider === "smtp" ? "SMTP" : "Gmail"} · {a.status === "connected" ? "Connected" : a.status === "revoked" ? "Needs reconnect" : a.status}</p>
                  {a.provider !== "smtp" && a.status === "connected" && !a.scopes.includes("gmail.readonly") && <p className="mt-1 text-xs text-amber-300">Reconnect Gmail to enable reply detection.</p>}
                  {a.provider !== "smtp" && a.status === "connected" && a.scopes.includes("gmail.readonly") && !a.scopes.includes("gmail.modify") && <p className="mt-1 text-xs text-amber-300">Reconnect Gmail to enable inbox actions.</p>}
                </div>
                <span className={`h-2 w-2 shrink-0 rounded-full ${a.status === "connected" ? "bg-emerald-400" : "bg-amber-400"}`} title={a.status} aria-label={a.status} />
                <div className="flex flex-wrap gap-2">
                  <button type="button" onClick={() => testSend.mutate(a.id)} disabled={a.status !== "connected" || testSend.isPending} className="min-h-10 flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-xs text-slate-200 hover:bg-white/[0.05] disabled:opacity-50">
                    <Send className="h-3.5 w-3.5" />{testSend.isPending && testSend.variables === a.id ? "Sending…" : "Send test"}
                  </button>
                  <button type="button" onClick={() => disconnect.mutate(a.id)} disabled={disconnect.isPending} className="min-h-10 flex items-center gap-1.5 rounded-lg border border-white/10 px-3 py-1.5 text-xs text-rose-300 hover:bg-rose-500/10 disabled:opacity-50">
                    <Trash2 className="h-3.5 w-3.5" />Disconnect
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {accounts.data && accounts.data.length === 0 && <p className="mt-4 text-sm text-slate-400">No sending accounts connected yet.</p>}
      </div>

      {/* API key */}
      <div className="mt-5 ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <KeyRound className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">API key</h2>
            <p className="text-[12.5px] text-slate-500">
              Only enter a key if your administrator provides one. This setting is saved in this browser.
            </p>
          </div>
        </div>
        <div className="mt-4 flex gap-2">
          <input
            type={showKey ? "text" : "password"}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="Leave blank if no key is configured"
            className="flex-1 bg-white/[0.04] border border-white/5 rounded-lg px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40"
          />
          <button
            onClick={() => setShowKey((v) => !v)}
            className="rounded-lg border border-white/5 px-3 text-[12.5px] text-slate-400 hover:bg-white/[0.04]"
          >
            {showKey ? "Hide" : "Show"}
          </button>
          <button
            onClick={save}
            className="rounded-lg bg-indigo-600 px-4 text-[13px] font-semibold text-white hover:bg-indigo-500"
          >
            {saved ? "Saved ✓" : "Save"}
          </button>
        </div>
      </div>

      {/* Connection */}
      <div className="mt-5 ui-panel rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <ShieldCheck className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">System status</h2>
            <p className="text-[12.5px] text-slate-500">Server availability is checked every 30 seconds.</p>
          </div>
        </div>
        <div className="mt-4 inline-flex"><ConnectionStatus /></div>
      </div>

      <div className="mt-5 flex items-start gap-2.5 text-[12px] text-slate-500">
        <Info className="w-4 h-4 shrink-0 mt-0.5" />
        <p>
          OAuth tokens are encrypted at rest on the server and never returned to
          this browser. Disconnecting deletes them permanently.
        </p>
      </div>
    </div>
  );
}
