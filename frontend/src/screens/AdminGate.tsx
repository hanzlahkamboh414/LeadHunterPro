import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { LockKeyhole, LogIn } from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { Spinner } from "../components/StatusChip";

/**
 * AdminGate — the secret /admin4269 password door.
 *
 * When login auth is OFF (the admin toggle), the site opens straight into
 * the normal user UI for everyone. The admin panel is reachable ONLY here:
 * the admin password unlocks the admin session; a wrong password leaves the
 * visitor in the normal user UI (link below).
 */
export default function AdminGate() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      // The secret URL names the account; the visitor only supplies the
      // password. A correct password = the real admin JWT session.
      await login("admin4269", password);
      navigate("/admin", { replace: true });
    } catch {
      setError("Wrong password. This site is in open mode — you can continue as a normal user below.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-screen w-full flex items-center justify-center bg-[#0B0E14] px-6">
      <div className="w-full max-w-sm">
        <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-6 shadow-2xl shadow-black/40">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-500/15 flex items-center justify-center shrink-0">
              <LockKeyhole className="w-[18px] h-[18px] text-indigo-400" strokeWidth={1.9} />
            </div>
            <div>
              <h1 className="text-[17px] font-semibold text-white">Admin access</h1>
              <p className="text-[12px] text-slate-500">Restricted area — password required.</p>
            </div>
          </div>

          <form onSubmit={submit} className="mt-5 space-y-3">
            <input
              type="password"
              required
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Admin password"
              className="w-full rounded-lg border border-white/5 bg-white/[0.04] px-3.5 py-2.5 text-[13px] text-slate-200 placeholder:text-slate-600 outline-none focus:border-indigo-500/40 focus:ring-2 focus:ring-indigo-500/40"
            />
            {error && (
              <p className="text-[12.5px] text-rose-300 bg-rose-500/10 rounded-lg px-3 py-2">{error}</p>
            )}
            <button
              type="submit"
              disabled={busy}
              className="w-full inline-flex items-center justify-center gap-2 rounded-lg bg-indigo-600 px-4 py-2.5 text-[13.5px] font-semibold text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {busy ? <Spinner /> : <LogIn className="w-4 h-4" />} Unlock admin panel
            </button>
          </form>
        </div>

        <p className="mt-4 text-center">
          <Link to="/" className="text-[12.5px] text-slate-500 hover:text-slate-300 hover:underline">
            ← Continue as normal user
          </Link>
        </p>
      </div>
    </div>
  );
}
