import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type SsoSettings = components["schemas"]["SsoSettingsResponse"];
export type UpsertSsoSettings = components["schemas"]["UpsertSsoSettingsRequest"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /platform/sso-settings — the current OIDC config, metadata only (the
 * client secret is never returned). `null` when nothing is configured yet
 * (backend 404) — distinct from an auth/server error. */
export async function getSsoSettings(): Promise<SsoSettings | null> {
  const { data, error, response } = await api.GET("/platform/sso-settings");
  if (response.status === 404) return null;
  if (error || !data) throw fail(error, "Failed to load SSO settings");
  return data;
}

/** PUT /platform/sso-settings — create or replace the singleton. Omit
 * `client_secret` to keep the current one — it is never revealed, so this
 * replaces, it does not merge. */
export async function upsertSsoSettings(payload: UpsertSsoSettings): Promise<SsoSettings> {
  const { data, error } = await api.PUT("/platform/sso-settings", { body: payload });
  if (error || !data) throw fail(error, "Failed to save SSO settings");
  return data;
}
