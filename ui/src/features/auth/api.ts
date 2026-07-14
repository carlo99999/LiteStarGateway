import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type CurrentUser = components["schemas"]["UserResponse"];
export type BrowserSession = components["schemas"]["BrowserSessionResponse"];

/** Extract a human-readable message from an openapi-fetch error payload. */
function messageFrom(error: unknown, fallback: string): string {
  if (error && typeof error === "object") {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

/** Establish a browser-only HttpOnly cookie session. */
export async function login(email: string, password: string): Promise<BrowserSession> {
  const { data, error } = await api.POST("/session/login", {
    body: { email, password },
  });
  if (error || !data) {
    throw new Error(messageFrom(error, "Invalid email or password"));
  }
  return data;
}

/** Restore and validate the cookie session after a reload. */
export async function fetchBrowserSession(): Promise<BrowserSession> {
  const { data, error } = await api.GET("/session");
  if (error || !data) {
    throw new Error(messageFrom(error, "Not authenticated"));
  }
  return data;
}

/** Revoke the session server-side and expire its HttpOnly cookie. */
export async function logout(): Promise<void> {
  const { error } = await api.POST("/session/logout");
  if (error) {
    throw new Error(messageFrom(error, "Logout failed"));
  }
}

/** POST /signup — redeem an invite token to create the account and set its
 * password. The invite determines which team (and role) the user joins. */
export async function signup(
  inviteToken: string,
  email: string,
  password: string,
): Promise<void> {
  const { error } = await api.POST("/signup", {
    body: { invite_token: inviteToken, email, password },
  });
  if (error) {
    throw new Error(messageFrom(error, "Signup failed — the invite may be invalid or expired"));
  }
}
