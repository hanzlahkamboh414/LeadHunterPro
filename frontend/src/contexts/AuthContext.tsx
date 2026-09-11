import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "../api/client";

interface AuthUser {
  id: string;
  username: string;
  is_admin: boolean;
}

interface AuthState {
  user: AuthUser | null;
  token: string | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  signup: (username: string, email: string, password: string) => Promise<void>;
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
  const [token, setToken] = useState<string | null>(() => {
    try {
      return localStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  });
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

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
          is_admin: Boolean(payload.is_admin),
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

  const storeToken = useCallback((t: string) => {
    localStorage.setItem(TOKEN_KEY, t);
    setToken(t);
  }, []);

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await api.login(username, password);
      storeToken(res.token);
    },
    [storeToken],
  );

  const signup = useCallback(
    async (username: string, email: string, password: string) => {
      const res = await api.signup(username, email, password);
      storeToken(res.token);
    },
    [storeToken],
  );

  const logout = useCallback(() => {
    // Record the logout in the admin activity log first (fire-and-forget —
    // the token is discarded either way, the log entry is best-effort).
    api.logout().catch(() => undefined);
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
  }, []);

  const resetPassword = useCallback(
    async (username: string, newPassword: string) => {
      await api.resetPassword(username, newPassword);
    },
    [],
  );

  const value = useMemo(
    () => ({ user, token, loading, login, signup, logout, resetPassword }),
    [user, token, loading, login, signup, logout, resetPassword],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
