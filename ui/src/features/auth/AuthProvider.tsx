import { useQueryClient } from "@tanstack/react-query";
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
  const queryClient = useQueryClient();
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
        queryClient.clear();
        setCsrfToken(null);
        setUser(null);
        setStatus("unauthenticated");
      });
  }, [queryClient]);

  const login = useCallback(async (email: string, password: string) => {
    const session = await loginRequest(email, password);
    queryClient.clear();
    setCsrfToken(session.csrf_token);
    setUser(session.user);
    setStatus("authenticated");
  }, [queryClient]);

  const logout = useCallback(async () => {
    await logoutRequest();
    queryClient.clear();
    setCsrfToken(null);
    setUser(null);
    setStatus("unauthenticated");
  }, [queryClient]);

  const value = useMemo(
    () => ({ status, user, login, logout }),
    [status, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
