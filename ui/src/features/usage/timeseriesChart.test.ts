import assert from "node:assert/strict";
import test from "node:test";
import {
  buildStackedSeries,
  maxStackedValue,
  totalMetric,
  type UsageBucketPoint,
} from "./timeseriesChart.ts";

function bucket(overrides: Partial<UsageBucketPoint>): UsageBucketPoint {
  return {
    bucket_start: "2026-01-01T00:00:00Z",
    request_count: 1,
    prompt_tokens: 10,
    completion_tokens: 5,
    total_tokens: 15,
    cost: 1.0,
    group_key: null,
    ...overrides,
  };
}

test("buildStackedSeries groups rows by bucket_start with one value per series", () => {
  const buckets = [
    bucket({ bucket_start: "2026-01-01T00:00:00Z", group_key: "fast", cost: 1.5 }),
    bucket({ bucket_start: "2026-01-01T00:00:00Z", group_key: "smart", cost: 3.0 }),
    bucket({ bucket_start: "2026-01-02T00:00:00Z", group_key: "fast", cost: 2.0 }),
  ];
  const series = buildStackedSeries(buckets, "cost");
  assert.deepEqual(series.seriesNames, ["fast", "smart"]);
  assert.deepEqual(series.points, [
    { bucketStart: "2026-01-01T00:00:00Z", values: { fast: 1.5, smart: 3.0 } },
    { bucketStart: "2026-01-02T00:00:00Z", values: { fast: 2.0 } },
  ]);
});

test("buildStackedSeries preserves first-seen series order, never re-sorted by value", () => {
  const buckets = [
    bucket({ group_key: "zeta", cost: 100 }),
    bucket({ group_key: "alpha", cost: 1 }),
  ];
  assert.deepEqual(buildStackedSeries(buckets, "cost").seriesNames, ["zeta", "alpha"]);
});

test("buildStackedSeries collapses an ungrouped series into a single 'total' key", () => {
  const buckets = [bucket({ group_key: null }), bucket({ group_key: null })];
  const series = buildStackedSeries(buckets, "calls");
  assert.deepEqual(series.seriesNames, ["total"]);
  assert.equal(series.points[0]?.values.total, 2);
});

test("buildStackedSeries switches metric between cost, calls and tokens", () => {
  const buckets = [
    bucket({ group_key: "fast", cost: 2, request_count: 3, total_tokens: 40 }),
  ];
  assert.equal(buildStackedSeries(buckets, "cost").points[0]?.values.fast, 2);
  assert.equal(buildStackedSeries(buckets, "calls").points[0]?.values.fast, 3);
  assert.equal(buildStackedSeries(buckets, "tokens").points[0]?.values.fast, 40);
});

test("totalMetric sums the full bucket list, not a paginated slice", () => {
  const buckets = [
    bucket({ cost: 1.25 }),
    bucket({ cost: 2.75 }),
    bucket({ cost: 0.5 }),
  ];
  assert.equal(totalMetric(buckets, "cost"), 4.5);
});

test("maxStackedValue is the tallest per-bucket stacked total across series", () => {
  const buckets = [
    bucket({ bucket_start: "2026-01-01T00:00:00Z", group_key: "fast", cost: 1 }),
    bucket({ bucket_start: "2026-01-01T00:00:00Z", group_key: "smart", cost: 2 }),
    bucket({ bucket_start: "2026-01-02T00:00:00Z", group_key: "fast", cost: 10 }),
  ];
  const series = buildStackedSeries(buckets, "cost");
  assert.equal(maxStackedValue(series), 10);
});

test("maxStackedValue is zero for an empty series", () => {
  assert.equal(maxStackedValue(buildStackedSeries([], "cost")), 0);
});
