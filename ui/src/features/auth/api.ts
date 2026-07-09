import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type CurrentUser = components["schemas"]["UserResponse"];
type TokenResponse = components["schemas"]["TokenResponse"];

/** Extract a human-readable message from an openapi-fetch error payload. */
function messageFrom(error: unknown, fallback: string): string {
  if (error && typeof error === "object") {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

/** POST /login — exchange email + password for a bearer JWT. */
export async function login(email: string, password: string): Promise<TokenResponse> {
  const { data, error } = await api.POST("/login", { body: { email, password } });
  if (error || !data) {
    throw new Error(messageFrom(error, "Invalid email or password"));
  }
  return data;
}

/** GET /me — the authenticated user (also validates the current token). */
export async function fetchCurrentUser(): Promise<CurrentUser> {
  const { data, error } = await api.GET("/me");
  if (error || !data) {
    throw new Error(messageFrom(error, "Not authenticated"));
  }
  return data;
}

/** POST /logout — revoke the session token (best-effort). */
export async function logout(): Promise<void> {
  await api.POST("/logout");
}
