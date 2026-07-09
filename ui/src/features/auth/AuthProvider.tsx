import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { setBearerToken } from "@/lib/api/client";
import { config } from "@/lib/config";
import { AuthContext, type AuthStatus } from "@/features/auth/auth-context";
import {
  fetchCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  type CurrentUser,
} from "@/features/auth/api";

function readStoredToken(): string | null {
  return window.localStorage.getItem(config.tokenStorageKey);
}

function storeToken(token: string | null): void {
  if (token) {
    window.localStorage.setItem(config.tokenStorageKey, token);
  } else {
    window.localStorage.removeItem(config.tokenStorageKey);
  }
  setBearerToken(token);
}

/**
 * Owns the bearer token + current user. On mount it restores a stored token and
 * validates it via GET /me; an invalid/expired token is cleared so the app
 * falls back to the login screen.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<CurrentUser | null>(null);

  useEffect(() => {
    const token = readStoredToken();
    if (!token) {
      setStatus("unauthenticated");
      return;
    }
    setBearerToken(token);
    fetchCurrentUser()
      .then((current) => {
        setUser(current);
        setStatus("authenticated");
      })
      .catch(() => {
        storeToken(null);
        setUser(null);
        setStatus("unauthenticated");
      });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const { access_token } = await loginRequest(email, password);
    storeToken(access_token);
    const current = await fetchCurrentUser();
    setUser(current);
    setStatus("authenticated");
  }, []);

  const logout = useCallback(async () => {
    try {
      await logoutRequest();
    } finally {
      storeToken(null);
      setUser(null);
      setStatus("unauthenticated");
    }
  }, []);

  const value = useMemo(
    () => ({ status, user, login, logout }),
    [status, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
