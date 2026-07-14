import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError } from "@/lib/api";
import type { SessionInfo } from "@/types/api";

interface AuthContextValue {
  session: SessionInfo | null;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    api
      .get<SessionInfo>("/api/auth/me")
      .then(setSession)
      .catch(() => setSession(null))
      .finally(() => setIsLoading(false));
  }, []);

  async function login(username: string, password: string) {
    await api.post("/api/auth/login", { username, password });
    const info = await api.get<SessionInfo>("/api/auth/me");
    setSession(info);
  }

  async function logout() {
    try {
      await api.post("/api/auth/logout");
    } catch (error) {
      // A 403 here (e.g. a stale/missing CSRF cookie) shouldn't trap
      // the user in a "can't log out" state — the session gets
      // cleared client-side either way, matching spec section 22:
      // no pretending an ambiguous outcome is a clean success either.
      if (!(error instanceof ApiError)) throw error;
    }
    setSession(null);
  }

  return (
    <AuthContext.Provider value={{ session, isLoading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
