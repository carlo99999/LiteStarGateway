import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { setCsrfToken } from "@/lib/api/client";
import { config } from "@/lib/config";
import { AuthContext, type AuthStatus } from "@/features/auth/auth-context";
import {
  fetchBrowserSession,
  login as loginRequest,
  logout as logoutRequest,
  type CurrentUser,
} from "@/features/auth/api";

/**
 * Owns the current browser session. The JWT is never exposed to JavaScript: the
 * browser sends its HttpOnly cookie, while only the CSRF value lives in memory.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<CurrentUser | null>(null);

  useEffect(() => {
    // One-way cleanup for sessions created before HttpOnly cookies shipped.
    window.localStorage.removeItem(config.legacyTokenStorageKey);
    fetchBrowserSession()
      .then((session) => {
        setCsrfToken(session.csrf_token);
        setUser(session.user);
        setStatus("authenticated");
      })
      .catch(() => {
        setCsrfToken(null);
        setUser(null);
        setStatus("unauthenticated");
      });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const session = await loginRequest(email, password);
    setCsrfToken(session.csrf_token);
    setUser(session.user);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    await logoutRequest();
    setCsrfToken(null);
    setUser(null);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo(
    () => ({ status, user, login, logout }),
    [status, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
