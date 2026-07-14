import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

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

/** GET /teams/{id}/keys — the team's API keys (active and revoked). */
export async function listTeamKeys(teamId: string): Promise<ApiKey[]> {
  const { data, error } = await api.GET("/teams/{team_id}/keys", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load API keys");
  return data;
}

/** POST /teams/{id}/keys — issue a personal (inference-scoped) key. The plaintext
 * is returned once and never again. */
export async function issueTeamKey(
  teamId: string,
  name: string | null,
  rateLimitRpm: number | null,
): Promise<IssuedKey> {
  const { data, error } = await api.POST("/teams/{team_id}/keys", {
    params: { path: { team_id: teamId } },
    body: { name, scope: "inference", rate_limit_rpm: rateLimitRpm },
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

/** GET /teams/{id}/service-principals — the team's service principals. */
export async function listServicePrincipals(teamId: string): Promise<ServicePrincipal[]> {
  const { data, error } = await api.GET("/teams/{team_id}/service-principals", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load service principals");
  return data;
}

/** POST /teams/{id}/service-principals/{spId}/keys — issue a key for an existing
 * service principal (the only kind that may hold management/all scope). */
export async function issueServicePrincipalKey(
  teamId: string,
  spId: string,
  name: string | null,
  scope: KeyScope,
  rateLimitRpm: number | null,
): Promise<IssuedKey> {
  const { data, error } = await api.POST(
    "/teams/{team_id}/service-principals/{sp_id}/keys",
    {
      params: { path: { team_id: teamId, sp_id: spId } },
      body: { name, scope, rate_limit_rpm: rateLimitRpm },
    },
  );
  if (error || !data) throw fail(error, "Failed to issue service-principal key");
  return data;
}
