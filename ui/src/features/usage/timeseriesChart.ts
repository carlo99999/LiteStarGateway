/** Pure data transforms for the usage timeseries chart (Plan 10 Phase 2).
 * No React/DOM here so these are covered by plain node:test unit tests,
 * mirroring `features/budgets/alertConfig.ts`'s convention — the chart
 * component itself is exercised manually via the dev server. */

export type ChartMetric = "cost" | "calls" | "tokens";

export const CHART_METRICS: readonly ChartMetric[] = ["cost", "calls", "tokens"];

/** The subset of `UsageBucketResponse` the chart/table need. */
export interface UsageBucketPoint {
  bucket_start: string;
  request_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost: number;
  group_key: string | null;
}

export interface StackedPoint {
  bucketStart: string;
  values: Record<string, number>;
}

export interface StackedSeries {
  /** Series (model) names in first-seen order — a fixed, non-cycled order
   * for categorical color assignment, never re-sorted by value. */
  seriesNames: string[];
  points: StackedPoint[];
}

function metricValue(bucket: UsageBucketPoint, metric: ChartMetric): number {
  if (metric === "cost") return bucket.cost;
  if (metric === "calls") return bucket.request_count;
  return bucket.total_tokens;
}

/** Group flat `(bucket_start, group_key)` rows (the `group_by=model` shape)
 * into one point per bucket, with each series's value keyed by name — the
 * shape a stacked-area chart draws directly. Buckets without a `group_key`
 * (no grouping requested) collapse into a single "total" series. */
export function buildStackedSeries(
  buckets: readonly UsageBucketPoint[],
  metric: ChartMetric,
): StackedSeries {
  const seriesNames: string[] = [];
  const byBucket = new Map<string, Record<string, number>>();
  for (const bucket of buckets) {
    const series = bucket.group_key ?? "total";
    if (!seriesNames.includes(series)) seriesNames.push(series);
    const values = byBucket.get(bucket.bucket_start) ?? {};
    values[series] = (values[series] ?? 0) + metricValue(bucket, metric);
    byBucket.set(bucket.bucket_start, values);
  }
  const points = Array.from(byBucket.entries())
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([bucketStart, values]) => ({ bucketStart, values }));
  return { seriesNames, points };
}

/** The headline total for the selected metric across every bucket — computed
 * from the full (non-paginated) bucket list, never by summing a table page. */
export function totalMetric(buckets: readonly UsageBucketPoint[], metric: ChartMetric): number {
  return buckets.reduce((sum, bucket) => sum + metricValue(bucket, metric), 0);
}

/** The largest single-bucket stacked total for the metric, i.e. the sum of
 * every series at one `bucketStart` — the y-axis ceiling for the chart. */
export function maxStackedValue(series: StackedSeries): number {
  let max = 0;
  for (const point of series.points) {
    const stacked = Object.values(point.values).reduce((sum, v) => sum + v, 0);
    if (stacked > max) max = stacked;
  }
  return max;
}
