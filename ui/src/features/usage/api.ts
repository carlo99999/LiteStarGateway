import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageResult } from "@/lib/api/pagination";
import type { UsageBucketPoint } from "@/features/usage/timeseriesChart";

export type TeamUsage = components["schemas"]["UsageResponse"];
export type TeamUsageTimeseries = components["schemas"]["UsageTimeseriesResponse"];

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

export type Granularity = "hour" | "day";

export interface UsageTimeseriesQuery {
  start: string; // RFC3339
  end: string; // RFC3339
  granularity: Granularity;
  /** Group by model (Plan 10 Phase 2) so one call returns one row per
   * `(bucket_start, model)` pair — the shape the stacked chart needs. */
  groupByModel: boolean;
  alias?: string;
  apiKeyId?: string;
}

/** GET /teams/{id}/usage/timeseries — bucketed usage, optionally grouped by
 * model (Plan 10 Phase 2's `group_by=model`). Not paginated: the bounded
 * `[start, end)` range already bounds the row count. */
export async function getTeamUsageTimeseries(
  teamId: string,
  query: UsageTimeseriesQuery,
): Promise<UsageBucketPoint[]> {
  const { data, error } = await api.GET("/teams/{team_id}/usage/timeseries", {
    params: {
      path: { team_id: teamId },
      query: {
        start: query.start,
        end: query.end,
        granularity: query.granularity,
        alias: query.alias || undefined,
        api_key_id: query.apiKeyId || undefined,
        group_by: query.groupByModel ? "model" : undefined,
      },
    },
  });
  if (error || !data) throw fail(error, "Failed to load usage timeseries");
  return data.buckets.map((bucket) => ({ ...bucket, group_key: bucket.group_key ?? null }));
}
