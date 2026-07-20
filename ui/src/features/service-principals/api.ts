import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageRequest, type PageResult } from "@/lib/api/pagination";

export type ServicePrincipal = components["schemas"]["ServicePrincipalResponse"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

async function requestServicePrincipals(
  teamId: string,
  request: PageRequest,
): Promise<ServicePrincipal[]> {
  const { data, error } = await api.GET("/teams/{team_id}/service-principals", {
    params: { path: { team_id: teamId }, query: request },
  });
  if (error || !data) throw fail(error, "Failed to load service principals");
  return data;
}

/** GET /teams/{id}/service-principals — one table page. */
export async function listServicePrincipalsPage(
  teamId: string,
  offset: number,
): Promise<PageResult<ServicePrincipal>> {
  const request = pageRequest(offset);
  return pageResult(await requestServicePrincipals(teamId, request), offset);
}

/** POST /teams/{id}/service-principals — create a team identity (no keys yet;
 * issue those from the API Keys page). */
export async function createServicePrincipal(
  teamId: string,
  name: string,
): Promise<ServicePrincipal> {
  const { data, error } = await api.POST("/teams/{team_id}/service-principals", {
    params: { path: { team_id: teamId } },
    body: { name },
  });
  if (error || !data) throw fail(error, "Failed to create service principal");
  return data;
}

/** PATCH /teams/{id}/service-principals/{spId} — enable or disable it. A disabled
 * principal's keys stop authenticating. */
export async function setServicePrincipalEnabled(
  teamId: string,
  spId: string,
  enabled: boolean,
): Promise<ServicePrincipal> {
  const { data, error } = await api.PATCH("/teams/{team_id}/service-principals/{sp_id}", {
    params: { path: { team_id: teamId, sp_id: spId } },
    body: { enabled },
  });
  if (error || !data) throw fail(error, "Failed to change service-principal status");
  return data;
}

/** DELETE /teams/{id}/service-principals/{spId} — delete it and revoke all of its
 * keys (no orphaned credentials). */
export async function deleteServicePrincipal(teamId: string, spId: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}/service-principals/{sp_id}", {
    params: { path: { team_id: teamId, sp_id: spId } },
  });
  if (error) throw fail(error, "Failed to delete service principal");
}
