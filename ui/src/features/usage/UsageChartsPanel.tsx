import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { DataTable, type Column } from "@/components/common/DataTable";
import { EmptyState } from "@/components/common/EmptyState";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { teamCacheSavings } from "@/features/models/api";
import { StackedAreaChart } from "@/features/usage/StackedAreaChart";
import { getTeamUsageTimeseries, type Granularity } from "@/features/usage/api";
import { getTeamBudget } from "@/features/teams/api";
import {
  buildStackedSeries,
  totalMetric,
  type ChartMetric,
  type UsageBucketPoint,
} from "@/features/usage/timeseriesChart";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

const GRANULARITIES: Granularity[] = ["hour", "day"];
const METRICS: { key: ChartMetric; label: string }[] = [
  { key: "cost", label: "cost" },
  { key: "calls", label: "calls" },
  { key: "tokens", label: "tokens" },
];

function formatUsd(cost: number): string {
  return `$${cost.toFixed(4)}`;
}

function formatCount(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

function formatMetric(metric: ChartMetric, value: number): string {
  return metric === "cost" ? formatUsd(value) : formatCount(value);
}

function toDatetimeLocal(date: Date): string {
  return date.toISOString().slice(0, 16);
}

function defaultRange(): { start: string; end: string } {
  const end = new Date();
  const start = new Date(end.getTime() - 7 * 24 * 60 * 60 * 1000);
  return { start: toDatetimeLocal(start), end: toDatetimeLocal(end) };
}

const TABLE_COLUMNS: Column<UsageBucketPoint & { rowKey: string }>[] = [
  {
    key: "bucket",
    header: "bucket",
    cell: (b) => (
      <span className="tabular text-xs text-foreground">
        {new Date(b.bucket_start).toISOString().replace("T", " ").slice(0, 16)}
      </span>
    ),
  },
  {
    key: "model",
    header: "model",
    cell: (b) => <span className="text-xs text-foreground">{b.group_key ?? "total"}</span>,
  },
  {
    key: "calls",
    header: "calls",
    numeric: true,
    cell: (b) => <span className="tabular text-xs text-muted-foreground">{b.request_count}</span>,
  },
  {
    key: "tokens",
    header: "tokens",
    numeric: true,
    cell: (b) => (
      <span className="tabular text-xs text-muted-foreground">
        {formatCount(b.total_tokens)}
      </span>
    ),
  },
  {
    key: "cost",
    header: "cost",
    numeric: true,
    cell: (b) => <span className="tabular text-xs text-foreground">{formatUsd(b.cost)}</span>,
  },
];

/** Overlay stat: budget cap + cache savings for the team, using EXISTING
 * endpoints only (Plan 10 Phase 2 scopes the overlay as opportunistic — no
 * new backend aggregation). Silent on error/absence, same convention as
 * `ModelsPage.tsx`'s `CacheSavingsPanel`. Per-router routing savings needs a
 * specific router id (no team-wide aggregate exists), so that overlay is
 * deliberately deferred rather than forced here. */
function OverlayStats({ teamId }: { teamId: string }) {
  const budget = useQuery({
    queryKey: ["teams", teamId, "budget"],
    queryFn: () => getTeamBudget(teamId),
    enabled: teamId.length > 0,
  });
  const cacheSavings = useQuery({
    queryKey: ["teams", teamId, "cache-savings"],
    queryFn: () => teamCacheSavings(teamId),
    enabled: teamId.length > 0,
  });

  const hasBudget = !budget.isError && budget.data;
  const hasCacheSavings = !cacheSavings.isError && (cacheSavings.data?.total_requests ?? 0) > 0;
  if (!hasBudget && !hasCacheSavings) return null;

  return (
    <div className="mb-4 grid gap-3 sm:grid-cols-3">
      {hasBudget ? (
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            // budget cap · {budget.data!.window}
          </p>
          <p className="tabular mt-1 text-lg text-foreground">
            {formatUsd(budget.data!.spent)} / {formatUsd(budget.data!.limit_cost)}
          </p>
        </div>
      ) : null}
      {hasCacheSavings ? (
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            // cache savings
          </p>
          <p className="tabular mt-1 text-lg text-foreground">
            {formatUsd(cacheSavings.data!.estimated_cost_saved)}
          </p>
        </div>
      ) : null}
    </div>
  );
}

/** Cost/token/call charts over time, grouped by model (Plan 10 Phase 2). Reuses
 * the Phase 1 `/usage/timeseries` endpoint's existing filter params, plus a
 * new `group_by=model` so the whole stacked chart comes from one call. */
export function UsageChartsPanel({ teamId }: { teamId: string }) {
  const [range, setRange] = useState(defaultRange);
  const [granularity, setGranularity] = useState<Granularity>("day");
  const [metric, setMetric] = useState<ChartMetric>("cost");
  const [alias, setAlias] = useState("");
  const [apiKeyId, setApiKeyId] = useState("");

  const startIso = range.start ? new Date(range.start).toISOString() : "";
  const endIso = range.end ? new Date(range.end).toISOString() : "";
  const rangeValid = startIso.length > 0 && endIso.length > 0 && startIso < endIso;

  const timeseries = useQuery({
    queryKey: [
      "teams",
      teamId,
      "usage",
      "timeseries",
      startIso,
      endIso,
      granularity,
      alias,
      apiKeyId,
    ],
    queryFn: () =>
      getTeamUsageTimeseries(teamId, {
        start: startIso,
        end: endIso,
        granularity,
        groupByModel: true,
        alias: alias || undefined,
        apiKeyId: apiKeyId || undefined,
      }),
    enabled: teamId.length > 0 && rangeValid,
  });

  const buckets = useMemo(() => timeseries.data ?? [], [timeseries.data]);
  const series = useMemo(() => buildStackedSeries(buckets, metric), [buckets, metric]);
  const total = useMemo(() => totalMetric(buckets, metric), [buckets, metric]);
  const tableRows = useMemo(
    () => buckets.map((b) => ({ ...b, rowKey: `${b.bucket_start}:${b.group_key ?? "total"}` })),
    [buckets],
  );

  const error = toError(timeseries.error);

  return (
    <div className="mt-8">
      <p className="mb-3 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // usage over time · which models, how often
      </p>

      <OverlayStats teamId={teamId} />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="usage-chart-start" className="text-xs">
            start
          </Label>
          <Input
            id="usage-chart-start"
            type="datetime-local"
            className="h-9 w-52 font-mono text-xs"
            value={range.start}
            onChange={(event) => setRange((prev) => ({ ...prev, start: event.target.value }))}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="usage-chart-end" className="text-xs">
            end
          </Label>
          <Input
            id="usage-chart-end"
            type="datetime-local"
            className="h-9 w-52 font-mono text-xs"
            value={range.end}
            onChange={(event) => setRange((prev) => ({ ...prev, end: event.target.value }))}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="usage-chart-granularity" className="text-xs">
            bucket
          </Label>
          <select
            id="usage-chart-granularity"
            className={SELECT_CLASS}
            value={granularity}
            onChange={(event) => setGranularity(event.target.value as Granularity)}
          >
            {GRANULARITIES.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="usage-chart-alias" className="text-xs">
            alias filter
          </Label>
          <Input
            id="usage-chart-alias"
            placeholder="e.g. fast"
            className="h-9 w-36 font-mono text-xs"
            value={alias}
            onChange={(event) => setAlias(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="usage-chart-key" className="text-xs">
            api key id
          </Label>
          <Input
            id="usage-chart-key"
            placeholder="uuid"
            className="h-9 w-44 font-mono text-xs"
            value={apiKeyId}
            onChange={(event) => setApiKeyId(event.target.value)}
          />
        </div>
        <div className="ml-auto flex gap-1 rounded-md border border-border p-1">
          {METRICS.map((m) => (
            <button
              key={m.key}
              type="button"
              className={`rounded px-2 py-1 font-mono text-xs ${
                metric === m.key
                  ? "bg-primary text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
              onClick={() => setMetric(m.key)}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {!rangeValid ? (
        <EmptyState title="invalid range" description="Start must be before end." />
      ) : timeseries.isError ? (
        <EmptyState
          title="request failed"
          description={error?.message ?? "Failed to load usage timeseries."}
          className="border-destructive/40"
        />
      ) : timeseries.isLoading ? (
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="h-56 animate-pulse rounded bg-muted" />
        </div>
      ) : buckets.length === 0 ? (
        <EmptyState
          title="no usage in range"
          description="Genuinely zero calls for this team, filter and date range — not an error."
        />
      ) : (
        <>
          <p className="mb-2 font-mono text-xs text-muted-foreground">
            total {metric}: <span className="text-foreground">{formatMetric(metric, total)}</span>
          </p>
          <StackedAreaChart
            series={series}
            granularity={granularity}
            formatValue={(v) => formatMetric(metric, v)}
          />
        </>
      )}

      {buckets.length > 0 ? (
        <div className="mt-4">
          <p className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            // accessible table view
          </p>
          <DataTable
            columns={TABLE_COLUMNS}
            rows={tableRows}
            rowKey={(row) => row.rowKey}
            emptyTitle="no usage"
          />
        </div>
      ) : null}
    </div>
  );
}
