import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";
import { Search, Mail, Phone, Layers } from "lucide-react";
import type { SignupCategory } from "../types";

const CATEGORY_OPTIONS: {
  value: SignupCategory;
  label: string;
  hint: string;
  icon: typeof Mail;
}[] = [
  {
    value: "emails",
    label: "Email leads",
    hint: "Verified emails of construction companies",
    icon: Mail,
  },
  {
    value: "phones",
    label: "Phone leads",
    hint: "Direct phone numbers from license boards",
    icon: Phone,
  },
  {
    value: "both",
    label: "Both",
    hint: "Emails + phone numbers in one account",
    icon: Layers,
  },
];

export default function Signup() {
  const { signup } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [category, setCategory] = useState<SignupCategory>("both");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (password !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }
    setLoading(true);
    try {
      await signup(username, email, password, name.trim(), category);
      navigate("/");
    } catch (err: any) {
      setError(err?.message || "Signup failed");
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
          onSubmit={handleSubmit}
          className="ui-panel bg-[#0D1017] border border-white/5 rounded-xl p-6 space-y-4"
        >
          <h1 className="text-lg font-semibold text-white mb-2">Create account</h1>

          {error && (
            <div className="bg-red-500/10 border border-red-500/20 text-red-400 text-sm rounded-lg px-3 py-2">
              {error}
            </div>
          )}

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
            />
          </div>

          <div>
            <label className="block text-[12px] text-slate-400 mb-1">Full name</label>
            <input
              type="text"
              aria-label="Full name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Shown at the top of the app"
              required
              className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
            />
          </div>

          <div>
            <label className="block text-[12px] text-slate-400 mb-1">Email</label>
            <input
              type="email"
              aria-label="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
            />
          </div>

          <div>
            <label className="block text-[12px] text-slate-400 mb-1">Password</label>
            <input
              type="password"
              aria-label="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
            />
          </div>

          <div>
            <label className="block text-[12px] text-slate-400 mb-1">Confirm password</label>
            <input
              type="password"
              aria-label="Confirm password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              className="w-full bg-white/[0.04] border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500/50"
            />
          </div>

          {/* P3 — the vertical question: which leads does this account want?
              Existing behavior (emails) stays available; the default is Both
              so nobody is locked out of what the platform already offered. */}
          <div>
            <label className="block text-[12px] text-slate-400 mb-2">
              Which leads do you want?
            </label>
            <div className="grid gap-2">
              {CATEGORY_OPTIONS.map(({ value, label, hint, icon: Icon }) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setCategory(value)}
                  className={`flex items-center gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors ${
                    category === value
                      ? "border-indigo-500/60 bg-indigo-500/10"
                      : "border-white/10 bg-white/[0.04] hover:border-white/20"
                  }`}
                >
                  <Icon
                    className={`w-4 h-4 shrink-0 ${
                      category === value ? "text-indigo-400" : "text-slate-500"
                    }`}
                  />
                  <span>
                    <span
                      className={`block text-[13px] font-medium ${
                        category === value ? "text-white" : "text-slate-300"
                      }`}
                    >
                      {label}
                    </span>
                    <span className="block text-[11.5px] text-slate-500">{hint}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 rounded-lg bg-indigo-500 hover:bg-indigo-400 text-white text-sm font-medium transition-colors disabled:opacity-50"
          >
            {loading ? "Creating account..." : "Create account"}
          </button>

          <div className="text-center text-[12px] text-slate-400 pt-1">
            Already have an account?{" "}
            <Link to="/login" className="hover:text-indigo-400 transition-colors">
              Sign in
            </Link>
          </div>
        </form>
      </div>
    </div>
  );
}
