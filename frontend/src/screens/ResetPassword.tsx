import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { Search } from "lucide-react";

export default function ResetPassword() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [step, setStep] = useState<1 | 2>(1);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [loading, setLoading] = useState(false);

  const handleUsernameSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setSuccess("");
    // Move to step 2 — we don't validate username existence until the actual reset
    setStep(2);
  };

  const handleResetSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (newPassword !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }
    if (newPassword.length < 4) {
      setError("Password must be at least 4 characters");
      return;
    }
    setLoading(true);
    try {
      await api.resetPassword(username, newPassword);
      setSuccess("Password reset successful! Redirecting to login...");
      setTimeout(() => navigate("/login"), 1500);
    } catch (err: any) {
      setError(err?.message || "Reset failed — check your username");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-page">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex items-center justify-center gap-2.5 mb-8">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center relative">
            <Search className="w-5 h-5 text-white" strokeWidth={2.5} />
            <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-emerald-400 ring-2 ring-[#0B0E14]" />
          </div>
          <div className="text-xl font-semibold text-white">
            LeadHunter <span className="text-indigo-400">Pro</span>
          </div>
        </div>

        <form
          onSubmit={step === 1 ? handleUsernameSubmit : handleResetSubmit}
          className="ui-panel bg-[#0D1017] border border-white/5 rounded-xl p-6 space-y-4"
        >
          <h1 className="text-lg font-semibold text-white mb-2">Reset password</h1>

          {error && (
            <div className="bg-red-500/10 border border-red-500/20 text-red-400 text-sm rounded-lg px-3 py-2">
              {error}
            </div>
          )}

          {success && (
            <div className="bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 text-sm rounded-lg px-3 py-2">
              {success}
            </div>
          )}

          {step === 1 ? (
            <>
              <div>
                <label className="block text-[12px] text-slate-400 mb-1">Username</label>
                <input
                  type="text"
                  aria-label="Username"
              value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                  autoFocus
                  className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
                  placeholder="Enter your username"
                />
              </div>
              <button
                type="submit"
                className="w-full py-2.5 rounded-lg bg-indigo-500 hover:bg-indigo-400 text-white text-sm font-medium transition-colors"
              >
                Continue
              </button>
            </>
          ) : (
            <>
              <div className="text-[13px] text-slate-400">
                Setting new password for <span className="text-white font-medium">{username}</span>
              </div>
              <div>
                <label className="block text-[12px] text-slate-400 mb-1">New password</label>
                <input
                  type="password"
                  aria-label="New password"
              value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  required
                  autoFocus
                  className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
                />
              </div>
              <div>
                <label className="block text-[12px] text-slate-400 mb-1">Confirm new password</label>
                <input
                  type="password"
                  aria-label="Confirm password"
              value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  required
                  className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
                />
              </div>
              <button
                type="submit"
                disabled={loading}
                className="w-full py-2.5 rounded-lg bg-indigo-500 hover:bg-indigo-400 text-white text-sm font-medium transition-colors disabled:opacity-50"
              >
                {loading ? "Resetting..." : "Reset password"}
              </button>
              <button
                type="button"
                onClick={() => setStep(1)}
                className="w-full text-center text-[12px] text-slate-400 hover:text-indigo-400 transition-colors"
              >
                Use a different username
              </button>
            </>
          )}

          <div className="text-center text-[12px] text-slate-400 pt-1">
            <Link to="/login" className="hover:text-indigo-400 transition-colors">
              Back to sign in
            </Link>
          </div>
        </form>
      </div>
    </div>
  );
}
