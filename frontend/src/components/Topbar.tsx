import { FormEvent, useEffect, useRef, useState } from "react";
import { useIsFetching, useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { Search, History, ArrowUpRight } from "lucide-react";
import { api } from "../api/client";
import { useAuth } from "../contexts/AuthContext";
import { canSeeEmails } from "../lib/verticals";

/** "Skye Schooly" -> "SS", "king" -> "KI" — the avatar badge initials. */
function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.trim().slice(0, 2).toUpperCase() || "U";
}

/** "just now" / "2m ago" for a millisecond epoch (react-query dataUpdatedAt). */
function msAgo(ms: number): string {
  const mins = Math.floor((Date.now() - ms) / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}

/** Live backend connection (the frontend1 design idea): an honest green/red
 *  dot + last-check time, polled every 30s — the user always knows whether
 *  the data on screen is live or the backend has gone away. */
export function ConnectionStatus() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.health(),
    refetchInterval: 30_000,
    staleTime: 25_000,
    retry: 1,
  });
  const fetching = useIsFetching({ queryKey: ["health"] }) > 0;
  const online = health.isSuccess;

  return (
    <div
      className="flex items-center gap-2 px-2.5 py-1.5 rounded-lg bg-white/[0.03] border border-white/5"
      role="status"
      title={health.isPending ? "Checking server connection" : online ? "Backend connection healthy" : "Backend unreachable — retrying every 30s"}
    >
      <span
        className={`w-2 h-2 rounded-full ${
          health.isPending ? "bg-amber-400" : online
            ? "bg-emerald-400"
            : "bg-rose-400 animate-pulse"
        } ${fetching ? "opacity-100" : "opacity-80"}`}
      />
      <span
        className={`text-[12px] font-medium hidden md:block ${
          online ? "text-slate-400" : "text-rose-300"
        }`}
      >
        {health.isPending ? "Checking" : online ? "Connected" : "Offline"}
      </span>
      <span className="sr-only md:hidden">{health.isPending ? "Checking connection" : online ? "Connected" : "Offline"}</span>
      {online && health.dataUpdatedAt > 0 && (
        <span className="text-[11px] text-slate-600 hidden lg:block">
          {msAgo(health.dataUpdatedAt)}
        </span>
      )}
    </div>
  );
}

export default function Topbar() {
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  const { user, tenantMode, tenants, activeTenantId, switchTenant } = useAuth();
  const emailsVisible = canSeeEmails(user?.category, user?.is_admin, Boolean(user));
  const location = useLocation();
  const searchRef = useRef<HTMLInputElement>(null);
  const titles: Record<string, string> = { "/": "Overview", "/research": "Discovery", "/phones": "Phone discovery", "/linkedin": "LinkedIn contacts", "/leads": "Companies", "/contacts": "Contacts", "/campaigns": "Campaigns", "/email": "Inbox", "/history": "Run history", "/settings": "Settings", "/admin": "Administration" };
  const pageTitle = titles[location.pathname] || (location.pathname.startsWith("/leads/") ? "Company intelligence" : "Workspace");
  useEffect(() => {
    document.title = `${pageTitle} · LeadHunter Pro`;
  }, [pageTitle]);
  useEffect(() => {
    const shortcut = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, []);

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const term = q.trim();
    navigate(term ? `/leads?q=${encodeURIComponent(term)}` : "/leads");
  }

  // Who is signed in — the account's display name (falls back to the
  // username), never a hardcoded one. In OPEN MODE (login auth off) there is
  // no session: the chip says so honestly instead of showing a blank user.
  const displayName = user?.name || user?.username || "Open access";
  const subtitle = !user
    ? "no login required"
    : user.is_admin
      ? "Administrator"
      : "Business Development";

  return (
    // shrink-0: the shell is a fixed height now, so the bar must keep its own
    // height instead of being squeezed by the scrolling <main> beside it.
    <header className="workspace-topbar shrink-0 flex items-center gap-4 border-b border-white/5">
      <div className="topbar-breadcrumb"><span>Workspace</span><span>/</span><strong>{pageTitle}</strong></div>
      {tenantMode && (
        <select
          aria-label="Active workspace"
          value={activeTenantId}
          onChange={(event) => switchTenant(event.target.value)}
          className="max-w-52 rounded-lg border border-white/10 bg-[#151923] px-2 py-2 text-xs text-slate-100"
        >
          {tenants.map((tenant) => (
            <option key={tenant.id} value={tenant.id}>{tenant.name}</option>
          ))}
        </select>
      )}
      {emailsVisible && <form onSubmit={onSubmit} role="search" className="workspace-search flex-1 max-w-xl relative">
        <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-500" />
        <input
          type="text"
          ref={searchRef}
          aria-label="Search companies and contacts"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search companies & contacts…"
          className="w-full bg-white/[0.04] border border-white/5 rounded-lg pl-10 pr-4 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:ring-2 focus:ring-indigo-500/40"
        />
      </form>}

      <ConnectionStatus />

      {emailsVisible && <button
        type="button"
        onClick={() => navigate("/history")}
        className="relative w-9 h-9 rounded-lg flex items-center justify-center text-slate-400 hover:bg-white/[0.04]"
        title="Recent activity"
        aria-label="Recent activity"
      >
        <History className="w-[18px] h-[18px]" strokeWidth={1.75} />
      </button>}

      <button type="button" onClick={() => navigate(emailsVisible ? "/settings" : "/phones")} aria-label={emailsVisible ? "Account settings" : "Phone workspace"} className="account-button flex items-center gap-2.5 pl-1 pr-2 py-1 rounded-lg hover:bg-white/[0.04]">
        <div className="w-8 h-8 rounded-full bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center text-[12px] font-semibold text-white">
          {initials(displayName)}
        </div>
        <div className="text-left leading-tight hidden sm:block">
          <div className="text-[13px] text-white font-medium">{displayName}</div>
          <div className="text-[11px] text-slate-500">{subtitle}</div>
        </div>
        <ArrowUpRight className="w-3.5 h-3.5 text-slate-500 hidden xl:block" />
      </button>
    </header>
  );
}
