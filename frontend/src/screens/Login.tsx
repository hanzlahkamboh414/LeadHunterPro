import { useState } from "react";
import { Link, useNavigate, Navigate } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";
import { Search } from "lucide-react";

export default function Login() {
  const { login, authEnabled } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // Open mode (admin toggled the login page off): the login page is dead —
  // bounce straight into the app.
  if (authEnabled === false) {
    return <Navigate to="/" replace />;
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(username, password);
      navigate("/");
    } catch (err: any) {
      setError(err?.message || "Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0B0E14] px-4">
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
          onSubmit={handleSubmit}
          className="bg-[#0D1017] border border-white/5 rounded-xl p-6 space-y-4"
        >
          <h1 className="text-lg font-semibold text-white mb-2">Sign in</h1>

          {error && (
            <div className="bg-red-500/10 border border-red-500/20 text-red-400 text-sm rounded-lg px-3 py-2">
              {error}
            </div>
          )}

          <div>
            <label className="block text-[12px] text-slate-400 mb-1">Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoFocus
              className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
            />
          </div>

          <div>
            <label className="block text-[12px] text-slate-400 mb-1">Password</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
            />
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 rounded-lg bg-indigo-500 hover:bg-indigo-400 text-white text-sm font-medium transition-colors disabled:opacity-50"
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>

          <div className="flex items-center justify-between text-[12px] text-slate-400 pt-1">
            <Link to="/reset-password" className="hover:text-indigo-400 transition-colors">
              Forgot password?
            </Link>
            <Link to="/signup" className="hover:text-indigo-400 transition-colors">
              Create account
            </Link>
          </div>
        </form>

        {/* Public legal pages (E6 Google verification) — linked from the
            page Google's consent screen points at. */}
        <p className="mt-5 text-center text-[12px] text-slate-500">
          <Link to="/privacy" className="hover:text-indigo-400 transition-colors">
            Privacy Policy
          </Link>
          <span className="mx-2">·</span>
          <Link to="/terms" className="hover:text-indigo-400 transition-colors">
            Terms of Service
          </Link>
        </p>
      </div>
    </div>
  );
}
