import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import Admin from "./screens/Admin";
import "@fontsource-variable/inter";
import "./index.css";

// Local Vite preview only. No API request reaches the real backend.
type DemoUser = {
  id: string; username: string; name: string; email: string;
  is_admin: boolean; created_at: string; phone_daily_limit: number;
  phone_daily_used: number;
};
type DemoTenant = { id: string; name: string };
type DemoMember = { user_id: string; username: string; role: "owner" | "member" };

const users: DemoUser[] = [
  { id: "demo-admin", username: "demo_admin", name: "Demo Admin", email: "admin@local.test", is_admin: true, created_at: "2026-09-25", phone_daily_limit: 600, phone_daily_used: 0 },
  { id: "demo-caller", username: "demo_caller", name: "Demo Caller", email: "caller@local.test", is_admin: false, created_at: "2026-09-25", phone_daily_limit: 600, phone_daily_used: 0 },
];
const tenants: DemoTenant[] = [{ id: "the-best-estimators-llc", name: "The Best Estimators LLC" }];
const memberships = new Map<string, DemoMember[]>([
  ["the-best-estimators-llc", [
    { user_id: "demo-admin", username: "demo_admin", role: "owner" },
    { user_id: "demo-caller", username: "demo_caller", role: "member" },
  ]],
]);

function reply(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(raw, window.location.origin);
  const path = url.pathname.replace(/^\/api\/v1/, "");
  const method = (init?.method || "GET").toUpperCase();

  if (path === "/auth/mode") return reply({ auth_enabled: true, tenant_mode: true });
  if (path === "/admin/users" && method === "GET") return reply({ total: users.length, users });
  if (path === "/admin/users" && method === "POST") {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (!tenants.some((tenant) => tenant.id === body.tenant_id)) return reply({ detail: "Choose a valid tenant" }, 422);
    if (users.some((user) => user.username === body.username)) return reply({ detail: "Username already taken" }, 409);
    const created: DemoUser = {
      id: crypto.randomUUID(), username: body.username, name: body.name || body.username,
      email: body.email, is_admin: false, created_at: new Date().toISOString(),
      phone_daily_limit: 600, phone_daily_used: 0,
    };
    users.push(created);
    memberships.get(body.tenant_id)?.push({ user_id: created.id, username: created.username, role: "member" });
    return reply(created, 201);
  }
  if (path === "/admin/tenants" && method === "GET") {
    return reply(tenants.map((tenant) => ({
      ...tenant, member_count: memberships.get(tenant.id)?.length ?? 0,
    })).sort((a, b) => a.name.localeCompare(b.name)));
  }
  if (path === "/admin/tenants" && method === "POST") {
    const body = JSON.parse(String(init?.body ?? "{}"));
    const name = String(body.name ?? "").trim();
    if (!name) return reply({ detail: "Tenant name is required" }, 422);
    if (tenants.some((tenant) => tenant.name.toLowerCase() === name.toLowerCase())) {
      return reply({ detail: "Tenant name already exists" }, 409);
    }
    const tenant = { id: crypto.randomUUID(), name };
    tenants.push(tenant);
    memberships.set(tenant.id, [{ user_id: "demo-admin", username: "demo_admin", role: "owner" }]);
    return reply({ ...tenant, member_count: 1 }, 201);
  }
  const memberPath = path.match(/^\/admin\/tenants\/([^/]+)\/members(?:\/([^/]+))?$/);
  if (memberPath) {
    const tenantId = decodeURIComponent(memberPath[1]);
    const rows = memberships.get(tenantId);
    if (!rows) return reply({ detail: "Tenant not found" }, 404);
    if (!memberPath[2] && method === "GET") return reply(rows);
    const userId = decodeURIComponent(memberPath[2] ?? "");
    if (method === "PUT") {
      const user = users.find((item) => item.id === userId);
      if (!user) return reply({ detail: "User not found" }, 404);
      if (!rows.some((item) => item.user_id === userId)) rows.push({ user_id: userId, username: user.username, role: "member" });
      return reply({ tenant_id: tenantId, user_id: userId, role: "member" });
    }
    if (method === "DELETE") {
      const index = rows.findIndex((item) => item.user_id === userId);
      if (index < 0) return reply({ detail: "Membership not found" }, 404);
      if (rows[index].role === "owner") return reply({ detail: "Cannot remove owner" }, 409);
      rows.splice(index, 1);
      return reply({ removed: true });
    }
  }
  if (path === "/admin/activity") return reply({ total: 0, activity: [] });
  if (path === "/gmail/mode") return reply({ inbox_enabled: true, export_enabled: true });
  if (path === "/admin/phones/claims-report") return reply({ total_claims: 0, total_hidden: 0, by_user: [] });
  if (path === "/admin/phones/wrong") return reply([]);
  return reply({ detail: "This local demo only supports tenant management" }, 404);
};

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <div className="min-h-screen bg-[#0B0E14] text-slate-200">
        <div className="sticky top-0 z-50 border-b border-amber-400/30 bg-amber-950 px-5 py-3 text-sm text-amber-100">
          LOCAL DEMO — data_source=fixture · bridge_mode=true · fallback_reason=tenant mode safety gate remains closed.
          Changes stay in this browser tab and reset on refresh. No real database is connected.
        </div>
        <main className="mx-auto max-w-7xl p-5">
          <Admin initialTab="users" />
        </main>
      </div>
    </QueryClientProvider>
  </React.StrictMode>,
);
