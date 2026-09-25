import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import {
  ACTIVE_TENANT_KEY, chooseTenant, tenantStorageKey,
} from "../lib/tenantSelection";
import type { TenantMembership } from "../lib/tenantSelection";

interface AuthUser {
  id: string;
  username: string;
  name: string;
  is_admin: boolean;
  /** Which verticals the account uses (P3): "emails" | "phones" | "both".
   *  Tokens minted before P3 carry no claim — they default to "both", the
   *  same default the backend's additive migration gives existing rows. */
  category: "emails" | "phones" | "both";
}

interface AuthState {
  user: AuthUser | null;
  token: string | null;
  loading: boolean;
  /** Is the login page ON? null while the boot check is in flight. When
   *  false (admin toggled it off) the site opens straight into the normal
   *  user UI — the admin panel is reachable only via /admin4269. */
  authEnabled: boolean | null;
  tenantMode: boolean;
  tenants: TenantMembership[];
  activeTenantId: string;
  tenantError: string;
  switchTenant: (tenantId: string) => void;
  login: (username: string, password: string) => Promise<void>;
  signup: (
    username: string,
    email: string,
    password: string,
    name?: string,
    category?: "emails" | "phones" | "both",
  ) => Promise<void>;
  logout: () => void;
  resetPassword: (username: string, newPassword: string) => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

const TOKEN_KEY = "leadhunter.jwt";

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const base64 = token.split(".")[1];
    const json = atob(base64.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json);
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [token, setToken] = useState<string | null>(() => {
    try {
      return localStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  });
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState<boolean | null>(null);
  const [tenantMode, setTenantMode] = useState<boolean | null>(null);
  const [tenants, setTenants] = useState<TenantMembership[]>([]);
  const [activeTenantId, setActiveTenantId] = useState("");
  const [tenantLoading, setTenantLoading] = useState(false);
  const [tenantChecked, setTenantChecked] = useState(false);
  const [tenantError, setTenantError] = useState("");

  // Boot check: is the login page on or off? Fail-SAFE — if the check can't
  // reach the backend, assume login is required (the protected default).
  useEffect(() => {
    let cancelled = false;
    api
      .authMode()
      .then((r) => {
        if (!cancelled) {
          setAuthEnabled(r.auth_enabled);
          setTenantMode(r.tenant_mode === true);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAuthEnabled(true);
          setTenantMode(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // On mount (or token change), decode user from JWT payload.
  useEffect(() => {
    if (!token) {
      setUser(null);
      setLoading(false);
      return;
    }
    const payload = decodeJwtPayload(token);
    if (payload && payload.user_id && payload.exp) {
      const expiresAt = (payload.exp as number) * 1000;
      if (Date.now() < expiresAt) {
        setUser({
          id: payload.user_id as string,
          username: (payload.username as string) || "",
          name: (payload.name as string) || "",
          is_admin: Boolean(payload.is_admin),
          category: (payload.category as AuthUser["category"]) || "both",
        });
      } else {
        // Token expired
        localStorage.removeItem(TOKEN_KEY);
        setToken(null);
        setUser(null);
      }
    } else {
      localStorage.removeItem(TOKEN_KEY);
      setToken(null);
      setUser(null);
    }
    setLoading(false);
  }, [token]);

  useEffect(() => {
    let cancelled = false;
    if (!token || !user || !tenantMode) {
      setTenants([]);
      setActiveTenantId("");
      setTenantError("");
      setTenantChecked(false);
      try { localStorage.removeItem(ACTIVE_TENANT_KEY); } catch { /* private mode */ }
      return;
    }
    setTenantLoading(true);
    setTenantChecked(false);
    api.myTenants()
      .then((memberships) => {
        if (cancelled) return;
        let saved = "";
        try { saved = localStorage.getItem(tenantStorageKey(user.id)) || ""; } catch { /* private mode */ }
        const selected = chooseTenant(memberships, saved);
        setTenants(memberships);
        setActiveTenantId(selected);
        setTenantError("");
        try {
          if (selected) {
            localStorage.setItem(ACTIVE_TENANT_KEY, selected);
            localStorage.setItem(tenantStorageKey(user.id), selected);
          } else {
            localStorage.removeItem(ACTIVE_TENANT_KEY);
          }
        } catch { /* private mode */ }
      })
      .catch(() => {
        if (cancelled) return;
        setTenants([]);
        setActiveTenantId("");
        setTenantError("Workspaces could not be loaded. Please try signing in again.");
        try { localStorage.removeItem(ACTIVE_TENANT_KEY); } catch { /* private mode */ }
      })
      .finally(() => {
        if (!cancelled) {
          setTenantLoading(false);
          setTenantChecked(true);
        }
      });
    return () => { cancelled = true; };
  }, [token, user?.id, tenantMode]);

  const switchTenant = useCallback((tenantId: string) => {
    if (!user || !tenants.some((tenant) => tenant.id === tenantId)) return;
    try {
      localStorage.setItem(ACTIVE_TENANT_KEY, tenantId);
      localStorage.setItem(tenantStorageKey(user.id), tenantId);
    } catch { /* private mode */ }
    queryClient.clear();
    setActiveTenantId(tenantId);
    window.location.reload();
  }, [queryClient, tenants, user]);

  const storeToken = useCallback((t: string) => {
    try { localStorage.removeItem(ACTIVE_TENANT_KEY); } catch { /* private mode */ }
    queryClient.clear();
    localStorage.setItem(TOKEN_KEY, t);
    setUser(null);
    setTenantChecked(false);
    setToken(t);
  }, [queryClient]);

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await api.login(username, password);
      storeToken(res.token);
    },
    [storeToken],
  );

  const signup = useCallback(
    async (
      username: string,
      email: string,
      password: string,
      name = "",
      category: "emails" | "phones" | "both" = "both",
    ) => {
      const res = await api.signup(username, email, password, name, category);
      storeToken(res.token);
    },
    [storeToken],
  );

  const logout = useCallback(() => {
    // Record the logout in the admin activity log first (fire-and-forget —
    // the token is discarded either way, the log entry is best-effort).
    api.logout().catch(() => undefined);
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(ACTIVE_TENANT_KEY);
    queryClient.clear();
    setToken(null);
    setUser(null);
    setTenants([]);
    setActiveTenantId("");
    setTenantChecked(false);
  }, [queryClient]);

  const resetPassword = useCallback(
    async (username: string, newPassword: string) => {
      await api.resetPassword(username, newPassword);
    },
    [],
  );

  // The app can't route until BOTH the token check and the auth-mode check
  // are done — routing on a half-known state would flash the login page on
  // an open site.
  const bootLoading = loading || authEnabled === null || tenantMode === null ||
    tenantLoading || (tenantMode && Boolean(user) && !tenantChecked);

  const value = useMemo(
    () => ({ user, token, loading: bootLoading, authEnabled,
      tenantMode: tenantMode === true, tenants, activeTenantId, tenantError, switchTenant,
      login, signup, logout, resetPassword }),
    [user, token, bootLoading, authEnabled, tenantMode, tenants, activeTenantId,
      tenantError, switchTenant, login, signup, logout, resetPassword],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
