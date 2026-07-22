import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageResult } from "@/lib/api/pagination";

export type Router = components["schemas"]["RouterResponse"];
export type RouterRequest = components["schemas"]["RouterRequest"];
export type CandidateRequest = components["schemas"]["CandidateRequest"];
export type Decision = components["schemas"]["DecisionResponse"];
export type RouterGrant = components["schemas"]["RouterGrantResponse"];
export type CallableRouter = components["schemas"]["CallableRouterResponse"];

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

/** GET /teams/{id}/routers/callable — own + extended + global routers. */
export async function listCallableRouters(teamId: string): Promise<CallableRouter[]> {
  const { data, error } = await api.GET("/teams/{team_id}/routers/callable", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load routers");
  return data;
}

/** GET /platform/routers — global (platform) routers. */
export async function listGlobalRouters(): Promise<Router[]> {
  const { data, error } = await api.GET("/platform/routers", {});
  if (error || !data) throw fail(error, "Failed to load global routers");
  return data;
}

/** POST /platform/routers — create a global router callable by every team. */
export async function createGlobalRouter(body: RouterRequest): Promise<Router> {
  const { data, error } = await api.POST("/platform/routers", { body });
  if (error || !data) throw fail(error, "Failed to create global router");
  return data;
}

/** PUT /platform/routers/{id} — replace a global router definition. */
export async function replaceGlobalRouter(routerId: string, body: RouterRequest): Promise<Router> {
  const { data, error } = await api.PUT("/platform/routers/{router_id}", {
    params: { path: { router_id: routerId } },
    body,
  });
  if (error || !data) throw fail(error, "Failed to update global router");
  return data;
}

/** Toggle a global router's `enabled` by replaying its definition. */
export async function setGlobalRouterEnabled(router: Router, enabled: boolean): Promise<Router> {
  return replaceGlobalRouter(router.id, {
    name: router.name,
    candidates: router.candidates as unknown as CandidateRequest[],
    default_model: router.default_model,
    strategy: router.strategy,
    strategy_config: router.strategy_config,
    enabled,
    shadow_strategy: router.shadow_strategy,
  });
}

/** DELETE /platform/routers/{id}. */
export async function deleteGlobalRouter(routerId: string): Promise<void> {
  const { error } = await api.DELETE("/platform/routers/{router_id}", {
    params: { path: { router_id: routerId } },
  });
  if (error) throw fail(error, "Failed to delete global router");
}

/** POST /platform/routers/{id}/make-global — promote a team router. */
export async function makeRouterGlobal(routerId: string): Promise<Router> {
  const { data, error } = await api.POST("/platform/routers/{router_id}/make-global", {
    params: { path: { router_id: routerId } },
  });
  if (error || !data) throw fail(error, "Failed to make the router global");
  return data;
}

interface RouterEgressAcknowledgements {
  activePromptEgress: boolean;
  shadowPromptEgress: boolean;
}

/** POST /platform/routers/{id}/extend — share a team router with other teams. */
export async function extendRouter(
  routerId: string,
  teamIds: string[],
  acknowledgements: RouterEgressAcknowledgements,
): Promise<RouterGrant[]> {
  const { data, error } = await api.POST("/platform/routers/{router_id}/extend", {
    params: { path: { router_id: routerId } },
    body: {
      team_ids: teamIds,
      ack_active_prompt_egress: acknowledgements.activePromptEgress,
      ack_shadow_prompt_egress: acknowledgements.shadowPromptEgress,
    },
  });
  if (error || !data) throw fail(error, "Failed to extend the router");
  return data;
}

/** GET /platform/routers/{id}/grants — teams a router is extended to. */
export async function listRouterGrants(routerId: string, signal?: AbortSignal): Promise<RouterGrant[]> {
  const { data, error } = await api.GET("/platform/routers/{router_id}/grants", {
    params: { path: { router_id: routerId } },
    signal,
  });
  if (error || !data) throw fail(error, "Failed to load extensions");
  return data;
}

/** DELETE /platform/routers/grants/{id} — revoke an extension. */
export async function unextendRouter(grantId: string): Promise<void> {
  const { error } = await api.DELETE("/platform/routers/grants/{grant_id}", {
    params: { path: { grant_id: grantId } },
  });
  if (error) throw fail(error, "Failed to revoke the extension");
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
