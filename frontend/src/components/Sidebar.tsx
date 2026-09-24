import { useEffect, useRef, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
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
  Mail,
  Menu,
  X,
  ArrowUpRight,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import { canSeeEmails, canSeePhones } from "../lib/verticals";

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
  // Phase E7: the connected Gmail's mail inside the app — real backend (the
  // /gmail API), so it belongs in the nav (CLAUDE.md §1 satisfied).
  { to: "/email", label: "Email", icon: Mail, end: false, adminOnly: false },
  { to: "/history", label: "Run History", icon: HistoryIcon, end: false, adminOnly: false },
  { to: "/settings", label: "Settings", icon: Settings, end: false, adminOnly: false },
  { to: "/admin", label: "Admin", icon: ShieldCheck, end: false, adminOnly: true },
];

// P3: the Phones vertical is category-gated (phones | both accounts, admin,
// and open-site mode where everyone runs as the shared "both" account).
// Shared with App.tsx so the route gate and the nav gate agree.
export default function Sidebar() {
  const { user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const drawer = useRef<HTMLDialogElement>(null);
  const location = useLocation();
  useEffect(() => { setOpen(false); }, [location.pathname, location.search]);
  useEffect(() => {
    if (open) drawer.current?.showModal();
    else drawer.current?.close();
  }, [open]);
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1024px)");
    const closeOnDesktop = () => { if (desktop.matches) setOpen(false); };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);

  const emailsVisible = canSeeEmails(user?.category, user?.is_admin, Boolean(user));
  const visibleNav = NAV.filter((item) =>
    (!item.adminOnly || user?.is_admin) &&
    (emailsVisible || item.to === "/admin"),
  );

  // Phones sits after Research — the two lead-hunting entry points together.
  const nav = [...visibleNav];
  if (canSeePhones(user?.category, user?.is_admin, Boolean(user))) {
    nav.splice(2, 0, { to: "/phones", label: "Phones", icon: Phone, end: false, adminOnly: false });
  }

  function rail(mobile = false) { return (
    // The shell is now a fixed viewport height, so the rail scrolls on its own
    // rather than clipping its nav on a short window.
    <aside className={`workspace-sidebar ${mobile ? "mobile-rail" : "desktop-rail"}`} aria-label="Workspace navigation">
      <div>
        <div className="flex items-center gap-2.5 px-2 pb-6 pt-1">
          <div className="brand-mark w-9 h-9 rounded-xl flex items-center justify-center relative shrink-0">
            <Search className="w-4 h-4 text-white" strokeWidth={2.5} />
          </div>
          <div className="leading-tight">
            <div className="text-[15px] font-semibold text-white">
              LeadHunter <span className="text-indigo-400">Pro</span>
            </div>
            <div className="text-[10px] tracking-wide text-slate-500">
              AI Sales Intelligence
            </div>
          </div>
          {mobile && <button type="button" className="ml-auto p-2 text-slate-400" aria-label="Close navigation" onClick={() => setOpen(false)}><X className="w-4 h-4" /></button>}
        </div>

        {emailsVisible && <NavLink to="/research" className="sidebar-create"><Search size={16} /> New research <ArrowUpRight size={15} className="ml-auto" /></NavLink>}

        <nav className="flex flex-col gap-0.5">
          {nav.map(({ to, label, icon: Icon, end }) => (
            <div key={to}>
            {to === "/" && <p className="nav-section-label">Discover</p>}
            {to === "/leads" && <p className="nav-section-label">Workspace</p>}
            {to === "/campaigns" && <p className="nav-section-label">Engage</p>}
            {to === "/history" && <p className="nav-section-label">Manage</p>}
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `sidebar-link flex items-center gap-3 px-3 py-2.5 rounded-lg text-[13.5px] transition-colors text-left ${
                  isActive
                    ? "bg-indigo-500/15 text-white"
                    : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200"
                }`
              }
            >
              <Icon className="w-[17px] h-[17px] shrink-0" strokeWidth={1.75} />
              {label}
            </NavLink>
            </div>
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

        <div className="sidebar-footer"><span>LeadHunter Pro</span><span>Sales intelligence</span></div>
      </div>
    </aside>
  ); }
  return <>
    {rail()}
    <button type="button" className="mobile-nav-toggle" aria-label="Open navigation" aria-expanded={open} onClick={() => setOpen(true)}><Menu size={21} /></button>
    <dialog ref={drawer} className="navigation-drawer" aria-label="Navigation" onCancel={() => setOpen(false)} onClick={(event) => { if (event.target === drawer.current) setOpen(false); }}>
      {rail(true)}
    </dialog>
  </>;
}
