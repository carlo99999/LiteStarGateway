import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import {
  fetchAllPages,
  pageRequest,
  pageResult,
  type PageRequest,
  type PageResult,
} from "@/lib/api/pagination";

export type ApiKey = components["schemas"]["KeyResponse"];
export type IssuedKey = components["schemas"]["CreatedKeyResponse"];
export type ServicePrincipal = components["schemas"]["ServicePrincipalResponse"];
export type KeyScope = "inference" | "management" | "all";

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

async function requestTeamKeys(teamId: string, request: PageRequest): Promise<ApiKey[]> {
  const { data, error } = await api.GET("/teams/{team_id}/keys", {
    params: { path: { team_id: teamId }, query: request },
  });
  if (error || !data) throw fail(error, "Failed to load API keys");
  return data;
}

/** GET /teams/{id}/keys — one table page (active and revoked). */
export async function listTeamKeysPage(
  teamId: string,
  offset: number,
): Promise<PageResult<ApiKey>> {
  const request = pageRequest(offset);
  return pageResult(await requestTeamKeys(teamId, request), offset);
}

/** POST /teams/{id}/keys — issue a personal (inference-scoped) key. The plaintext
 * is returned once and never again. */
export async function issueTeamKey(
  teamId: string,
  name: string | null,
  rateLimitRpm: number | null,
  expiresInDays: number | null,
): Promise<IssuedKey> {
  const { data, error } = await api.POST("/teams/{team_id}/keys", {
    params: { path: { team_id: teamId } },
    body: {
      name,
      scope: "inference",
      rate_limit_rpm: rateLimitRpm,
      expires_in_days: expiresInDays,
    },
  });
  if (error || !data) throw fail(error, "Failed to issue API key");
  return data;
}

/** DELETE /teams/{id}/keys/{keyId} — revoke a key. */
export async function revokeTeamKey(teamId: string, keyId: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}/keys/{key_id}", {
    params: { path: { team_id: teamId, key_id: keyId } },
  });
  if (error) throw fail(error, "Failed to revoke API key");
}

/** POST /teams/{id}/keys/{keyId}/rotate — issue a replacement key (same
 * scope/rate-limit/owner) and start the old key's grace window. Returns the new
 * plaintext once. */
export async function rotateTeamKey(teamId: string, keyId: string): Promise<IssuedKey> {
  const { data, error } = await api.POST("/teams/{team_id}/keys/{key_id}/rotate", {
    params: { path: { team_id: teamId, key_id: keyId } },
  });
  if (error || !data) throw fail(error, "Failed to rotate API key");
  return data;
}

async function requestServicePrincipals(
  teamId: string,
  request: PageRequest,
  signal?: AbortSignal,
): Promise<ServicePrincipal[]> {
  const { data, error } = await api.GET("/teams/{team_id}/service-principals", {
    params: { path: { team_id: teamId }, query: request },
    signal,
  });
  if (error || !data) throw fail(error, "Failed to load service principals");
  return data;
}

/** Complete team-scoped collection for the issue-key selector. */
export async function listAllServicePrincipals(
  teamId: string,
  signal?: AbortSignal,
): Promise<ServicePrincipal[]> {
  return fetchAllPages((request) => requestServicePrincipals(teamId, request, signal), {
    keyOf: (servicePrincipal) => servicePrincipal.id,
  });
}

/** POST /teams/{id}/service-principals/{spId}/keys — issue a key for an existing
 * service principal (the only kind that may hold management/all scope). */
export async function issueServicePrincipalKey(
  teamId: string,
  spId: string,
  name: string | null,
  scope: KeyScope,
  rateLimitRpm: number | null,
  expiresInDays: number | null,
): Promise<IssuedKey> {
  const { data, error } = await api.POST(
    "/teams/{team_id}/service-principals/{sp_id}/keys",
    {
      params: { path: { team_id: teamId, sp_id: spId } },
      body: { name, scope, rate_limit_rpm: rateLimitRpm, expires_in_days: expiresInDays },
    },
  );
  if (error || !data) throw fail(error, "Failed to issue service-principal key");
  return data;
}
