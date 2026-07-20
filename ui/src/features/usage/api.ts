import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageResult } from "@/lib/api/pagination";

export type TeamUsage = components["schemas"]["UsageResponse"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /teams/{id}/usage — one page of per-model token/cost totals. */
export async function listTeamUsagePage(
  teamId: string,
  offset: number,
): Promise<PageResult<TeamUsage>> {
  const request = pageRequest(offset);
  const { data, error } = await api.GET("/teams/{team_id}/usage", {
    params: { path: { team_id: teamId }, query: request },
  });
  if (error || !data) throw fail(error, "Failed to load usage");
  return pageResult(data, offset);
}
