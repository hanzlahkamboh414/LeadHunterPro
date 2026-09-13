import { NavLink } from "react-router-dom";
import {
  LayoutGrid,
  Building2,
  Users,
  Search,
  History as HistoryIcon,
  Settings,
  ShieldCheck,
  LogOut,
  Send,
  Phone,
  Briefcase,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";

// Nav items map to REAL, functional screens backed by the Leads API.
// Items with no backend data source (emails, pipeline, analytics…) are
// deliberately omitted rather than shown as fake/empty — see CLAUDE.md §1.
const NAV = [
  { to: "/", label: "Dashboard", icon: LayoutGrid, end: true, adminOnly: false },
  { to: "/research", label: "Research", icon: Search, end: false, adminOnly: false },
  // P4: LinkedIn is a research BYPRODUCT lane — every authenticated account
  // sees it (no signup-category gate, no quota). NOT part of the adminOnly /
  // category machinery above.
  { to: "/linkedin", label: "LinkedIn", icon: Briefcase, end: false, adminOnly: false },
  { to: "/leads", label: "Companies", icon: Building2, end: false, adminOnly: false },
  { to: "/contacts", label: "Contacts", icon: Users, end: false, adminOnly: false },
  { to: "/campaigns", label: "Campaigns", icon: Send, end: false, adminOnly: false },
  { to: "/history", label: "Run History", icon: HistoryIcon, end: false, adminOnly: false },
  { to: "/settings", label: "Settings", icon: Settings, end: false, adminOnly: false },
  { to: "/admin", label: "Admin", icon: ShieldCheck, end: false, adminOnly: true },
];

// P3: the Phones vertical is category-gated (phones | both accounts, admin,
// and open-site mode where everyone runs as the shared "both" account).
// Shared with App.tsx so the route gate and the nav gate agree.
export function canSeePhones(category?: string | null, isAdmin?: boolean, userExists?: boolean): boolean {
  if (!userExists) return true; // open-site mode (no session)
  return Boolean(isAdmin) || category === "phones" || category === "both";
}

export default function Sidebar() {
  const { user, logout } = useAuth();

  const visibleNav = NAV.filter(
    (item) => !item.adminOnly || user?.is_admin,
  );

  // Phones sits after Research — the two lead-hunting entry points together.
  const nav = [...visibleNav];
  if (canSeePhones(user?.category, user?.is_admin, Boolean(user))) {
    nav.splice(2, 0, { to: "/phones", label: "Phones", icon: Phone, end: false, adminOnly: false });
  }

  return (
    // The shell is now a fixed viewport height, so the rail scrolls on its own
    // rather than clipping its nav on a short window.
    <aside className="w-64 shrink-0 overflow-y-auto border-r border-white/5 bg-[#0D1017] flex flex-col justify-between px-4 py-5">
      <div>
        <div className="flex items-center gap-2.5 px-2 pb-6">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center relative shrink-0">
            <Search className="w-4 h-4 text-white" strokeWidth={2.5} />
            <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-emerald-400 ring-2 ring-[#0D1017]" />
          </div>
          <div className="leading-tight">
            <div className="text-[15px] font-semibold text-white">
              LeadHunter <span className="text-indigo-400">Pro</span>
            </div>
            <div className="text-[10px] tracking-wide text-slate-500">
              AI Sales Intelligence
            </div>
          </div>
        </div>

        <nav className="flex flex-col gap-0.5">
          {nav.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-lg text-[13.5px] transition-colors text-left ${
                  isActive
                    ? "bg-indigo-500/15 text-white"
                    : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200"
                }`
              }
            >
              <Icon className="w-[17px] h-[17px] shrink-0" strokeWidth={1.75} />
              {label}
            </NavLink>
          ))}
        </nav>
      </div>

      <div className="space-y-2">
        {/* User info + logout */}
        {user && (
          <div className="rounded-lg bg-white/[0.03] border border-white/5 px-3 py-2.5 flex items-center justify-between text-[12px] text-slate-400">
            <span className="truncate flex-1" title={user.username}>
              {user.name || user.username}
              {user.is_admin && (
                <span className="ml-1.5 text-indigo-400 text-[10px]">admin</span>
              )}
            </span>
            <button
              onClick={logout}
              className="shrink-0 ml-2 p-1 rounded hover:bg-white/10 text-slate-400 hover:text-white transition-colors"
              title="Sign out"
            >
              <LogOut className="w-3.5 h-3.5" />
            </button>
          </div>
        )}

        <div className="rounded-lg bg-white/[0.03] border border-white/5 px-3 py-2.5 flex items-center justify-between text-[12px] text-slate-400">
          <span className="flex items-center gap-1.5">
            <ShieldCheck className="w-3.5 h-3.5" />
            System status
          </span>
          <span className="flex items-center gap-1.5 text-emerald-400">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
            Operational
          </span>
        </div>
      </div>
    </aside>
  );
}
