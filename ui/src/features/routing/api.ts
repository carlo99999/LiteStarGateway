import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageResult } from "@/lib/api/pagination";

export type Router = components["schemas"]["RouterResponse"];
export type RouterRequest = components["schemas"]["RouterRequest"];
export type CandidateRequest = components["schemas"]["CandidateRequest"];
export type Decision = components["schemas"]["DecisionResponse"];

/** Shape of GET .../stats (untyped dict in the OpenAPI schema). */
export interface RouterStats {
  router: string;
  total: number;
  by_model: Record<string, number>;
  by_tier: Record<string, number>;
  shadow_by_model: Record<string, number>;
}

/** Shape of GET .../savings (untyped dict in the OpenAPI schema). */
export interface RouterSavings {
  router: string;
  estimated_savings: number;
  decisions_counted: number;
  decisions_without_usage: number;
}

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /teams/{id}/routers — the team's routers (no pagination upstream). */
export async function listRouters(teamId: string): Promise<Router[]> {
  const { data, error } = await api.GET("/teams/{team_id}/routers", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load routers");
  return data;
}

/** POST /teams/{id}/routers — create a smart router (virtual model). */
export async function createRouter(teamId: string, body: RouterRequest): Promise<Router> {
  const { data, error } = await api.POST("/teams/{team_id}/routers", {
    params: { path: { team_id: teamId } },
    body,
  });
  if (error || !data) throw fail(error, "Failed to create router");
  return data;
}

/** PUT /teams/{id}/routers/{routerId} — replace the definition. The only update
 * primitive (no PATCH), so enable/disable round-trips the full definition. */
export async function replaceRouter(
  teamId: string,
  routerId: string,
  body: RouterRequest,
): Promise<Router> {
  const { data, error } = await api.PUT("/teams/{team_id}/routers/{router_id}", {
    params: { path: { team_id: teamId, router_id: routerId } },
    body,
  });
  if (error || !data) throw fail(error, "Failed to update router");
  return data;
}

/** Toggle `enabled` by replaying the router's own definition with it flipped. */
export async function setRouterEnabled(
  teamId: string,
  router: Router,
  enabled: boolean,
): Promise<Router> {
  return replaceRouter(teamId, router.id, {
    name: router.name,
    // The response's candidate dicts are the request shape (same entity).
    candidates: router.candidates as unknown as CandidateRequest[],
    default_model: router.default_model,
    strategy: router.strategy,
    strategy_config: router.strategy_config,
    enabled,
    shadow_strategy: router.shadow_strategy,
  });
}

/** DELETE /teams/{id}/routers/{routerId}. */
export async function deleteRouter(teamId: string, routerId: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}/routers/{router_id}", {
    params: { path: { team_id: teamId, router_id: routerId } },
  });
  if (error) throw fail(error, "Failed to delete router");
}

/** GET .../stats — request distribution per chosen model / tier. */
export async function routerStats(teamId: string, routerId: string): Promise<RouterStats> {
  const { data, error } = await api.GET("/teams/{team_id}/routers/{router_id}/stats", {
    params: { path: { team_id: teamId, router_id: routerId } },
  });
  if (error || !data) throw fail(error, "Failed to load router stats");
  return data as unknown as RouterStats;
}

/** GET .../savings — estimated savings vs the priciest capable candidate. */
export async function routerSavings(teamId: string, routerId: string): Promise<RouterSavings> {
  const { data, error } = await api.GET("/teams/{team_id}/routers/{router_id}/savings", {
    params: { path: { team_id: teamId, router_id: routerId } },
  });
  if (error || !data) throw fail(error, "Failed to load router savings");
  return data as unknown as RouterSavings;
}

/** GET .../decisions — one page of recent routing decisions (most recent first). */
export async function listDecisionsPage(
  teamId: string,
  routerId: string,
  offset: number,
): Promise<PageResult<Decision>> {
  const request = pageRequest(offset);
  const { data, error } = await api.GET("/teams/{team_id}/routers/{router_id}/decisions", {
    params: { path: { team_id: teamId, router_id: routerId }, query: request },
  });
  if (error || !data) throw fail(error, "Failed to load decisions");
  return pageResult(data, offset);
}

/** Same-origin href for the JSONL decisions export (session cookie auth). */
export function decisionsExportHref(teamId: string, routerId: string): string {
  return `/teams/${teamId}/routers/${routerId}/decisions/export`;
}
