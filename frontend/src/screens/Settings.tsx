import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, ShieldCheck, Info, Mail, Send, Trash2, CheckCircle2, AlertTriangle } from "lucide-react";
import { ApiError, api, getStoredApiKey, googleAuthorizeUrl, setStoredApiKey } from "../api/client";
import { PageHeader } from "../components/PageHeader";

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
      setBanner({ ok: true, text: "Gmail account disconnected." });
      queryClient.invalidateQueries({ queryKey: ["email-accounts"] });
    },
  });

  const configured = gmailStatus.data?.configured === true;

  return (
    <div className="px-8 py-7 max-w-2xl">
      <PageHeader
        eyebrow="LeadHunter Pro"
        title="Settings"
        subtitle="API access, email sending, and connection details."
      />

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
      <div className="mt-6 rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <Mail className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">Email accounts</h2>
            <p className="text-[12.5px] text-slate-500">
              Connect Gmail via Google's official authorization page. Your Gmail
              password is never seen or stored — only an OAuth send permission.
            </p>
          </div>
        </div>

        {configured ? (
          <>
            <button
              onClick={() => {
                window.location.href = googleAuthorizeUrl();
              }}
              className="mt-4 rounded-lg bg-indigo-600 px-4 py-2 text-[13px] font-semibold text-white hover:bg-indigo-500"
            >
              + Connect Gmail
            </button>

            {accounts.isLoading && (
              <p className="mt-4 text-[13px] text-slate-500">Loading accounts…</p>
            )}

            {accounts.data && accounts.data.length > 0 && (
              <ul className="mt-4 divide-y divide-white/5">
                {accounts.data.map((a) => (
                  <li key={a.id} className="flex items-center gap-3 py-3">
                    <div className="flex-1 min-w-0">
                      <p className="text-[13.5px] text-slate-200 truncate">{a.email}</p>
                      <p className="text-[12px] text-slate-500">
                        {a.display_name ? `${a.display_name} · ` : ""}
                        {a.status === "connected"
                          ? "Connected"
                          : a.status === "revoked"
                            ? "Needs reconnect"
                            : a.status}
                      </p>
                      {a.status === "connected" &&
                        !a.scopes.includes("gmail.readonly") && (
                          <p className="mt-0.5 text-[11.5px] text-amber-400/90">
                            Reconnect to enable reply detection (follow-ups
                            stop when a lead answers)
                          </p>
                        )}
                      {a.status === "connected" &&
                        a.scopes.includes("gmail.readonly") &&
                        !a.scopes.includes("gmail.modify") && (
                          <p className="mt-0.5 text-[11.5px] text-amber-400/90">
                            Reconnect to enable star / mark-unread / trash /
                            archive in the Email screen
                          </p>
                        )}
                    </div>
                    <span
                      className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                        a.status === "connected" ? "bg-emerald-400" : "bg-amber-400"
                      }`}
                      title={a.status}
                    />
                    <button
                      onClick={() => testSend.mutate(a.id)}
                      disabled={testSend.isPending && testSend.variables === a.id}
                      className="flex items-center gap-1.5 rounded-lg border border-white/5 px-3 py-1.5 text-[12.5px] text-slate-300 hover:bg-white/[0.04] disabled:opacity-50"
                    >
                      <Send className="w-3.5 h-3.5" />
                      {testSend.isPending && testSend.variables === a.id ? "Sending…" : "Send test"}
                    </button>
                    <button
                      onClick={() => disconnect.mutate(a.id)}
                      disabled={disconnect.isPending && disconnect.variables === a.id}
                      className="flex items-center gap-1.5 rounded-lg border border-white/5 px-3 py-1.5 text-[12.5px] text-rose-400 hover:bg-rose-500/10 disabled:opacity-50"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                      Disconnect
                    </button>
                  </li>
                ))}
              </ul>
            )}

            {accounts.data && accounts.data.length === 0 && (
              <p className="mt-3 text-[12.5px] text-slate-500">
                No Gmail connected yet. Connect one to send campaigns in later
                phases — a test email proves the link end-to-end.
              </p>
            )}
          </>
        ) : (
          <div className="mt-4 flex items-start gap-2.5 rounded-lg border border-amber-500/20 bg-amber-500/10 px-4 py-3 text-[12.5px] text-amber-300">
            <Info className="w-4 h-4 shrink-0 mt-0.5" />
            <p>
              Gmail OAuth is not configured on the server yet —{" "}
              <code>GOOGLE_CLIENT_ID</code> / <code>GOOGLE_CLIENT_SECRET</code>{" "}
              must be set in the backend <code>.env</code> (Google Cloud project
              → Gmail API → OAuth client). Once configured, the Connect button
              appears here.
            </p>
          </div>
        )}
      </div>

      {/* API key */}
      <div className="mt-5 rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <KeyRound className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">API key</h2>
            <p className="text-[12.5px] text-slate-500">
              Only needed if the backend sets <code>LEADS_API_KEY</code>. Sent as{" "}
              <code>X-API-Key</code>. Stored in this browser (localStorage).
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
      <div className="mt-5 rounded-xl border border-white/5 bg-white/[0.02] p-5">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/15 flex items-center justify-center shrink-0">
            <ShieldCheck className="w-[17px] h-[17px] text-indigo-400" strokeWidth={1.75} />
          </div>
          <div>
            <h2 className="text-[15px] font-semibold text-white">System status</h2>
            <p className="text-[12.5px] text-slate-500">Backend connection is live.</p>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-1.5 text-[13px] text-emerald-400">
          <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
          Operational
        </div>
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
