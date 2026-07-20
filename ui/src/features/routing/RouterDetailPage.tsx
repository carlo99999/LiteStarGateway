import { useQuery } from "@tanstack/react-query";
import { getRouteApi, Link } from "@tanstack/react-router";
import { ArrowLeft, Download } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  decisionsExportHref,
  listDecisionsPage,
  listRouters,
  routerSavings,
  routerStats,
  type Decision,
} from "@/features/routing/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";

const route = getRouteApi("/app/routing/$teamId/$routerId");

function formatUsd(cost: number): string {
  return `$${cost.toFixed(4)}`;
}

function formatWhen(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // {label}
      </p>
      <p className="tabular mt-2 text-2xl text-foreground">{value}</p>
    </div>
  );
}

/** Top chosen models with counts, from the stats distribution. */
function Distribution({ label, entries }: { label: string; entries: Record<string, number> }) {
  const rows = Object.entries(entries).sort((a, b) => b[1] - a[1]);
  if (rows.length === 0) return null;
  const total = rows.reduce((sum, [, count]) => sum + count, 0);
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="mb-3 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // {label}
      </p>
      <div className="space-y-2">
        {rows.map(([name, count]) => (
          <div key={name} className="flex items-center gap-2 font-mono text-xs">
            <span className="w-44 truncate text-foreground">{name}</span>
            <span className="h-2 flex-1 overflow-hidden rounded bg-secondary">
              <span
                className="block h-full rounded bg-primary/60"
                style={{ width: `${Math.round((count / total) * 100)}%` }}
              />
            </span>
            <span className="tabular w-16 text-right text-muted-foreground">{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

const DECISION_COLUMNS: Column<Decision>[] = [
  {
    key: "when",
    header: "when",
    cell: (d) => <span className="text-xs text-muted-foreground">{formatWhen(d.created_at)}</span>,
  },
  {
    key: "model",
    header: "chosen model",
    cell: (d) => (
      <span className="flex items-center gap-1.5">
        <span className="text-foreground">{d.chosen_model}</span>
        {d.is_shadow ? <Badge variant="accent">shadow</Badge> : null}
        {d.fallback_used ? <Badge variant="warning">fallback</Badge> : null}
      </span>
    ),
  },
  { key: "strategy", header: "strategy", cell: (d) => <Badge variant="muted">{d.strategy}</Badge> },
  {
    key: "tier",
    header: "tier",
    cell: (d) => (
      <span className="text-xs text-muted-foreground">{d.tier?.toLowerCase() ?? "—"}</span>
    ),
  },
  {
    key: "score",
    header: "score",
    numeric: true,
    cell: (d) => (
      <span className="tabular text-xs text-muted-foreground">
        {d.score === null ? "—" : d.score.toFixed(2)}
      </span>
    ),
  },
  {
    key: "tokens",
    header: "tokens in/out",
    numeric: true,
    cell: (d) => (
      <span className="tabular text-xs text-muted-foreground">
        {d.prompt_tokens ?? "—"} / {d.completion_tokens ?? "—"}
      </span>
    ),
  },
  {
    key: "latency",
    header: "decision",
    numeric: true,
    cell: (d) => <span className="tabular text-xs text-muted-foreground">{d.decision_ms}ms</span>,
  },
];

/** One router: live definition summary, decision distribution, estimated
 * savings, and the recent decision log (with JSONL export). */
export function RouterDetailPage() {
  const { teamId, routerId } = route.useParams();
  const [offset, setOffset] = useState(0);

  // The list endpoint is the only definition read; find this router within it.
  const routers = useQuery({
    queryKey: ["team-routers", teamId],
    queryFn: () => listRouters(teamId),
  });
  const router = routers.data?.find((r) => r.id === routerId);

  const stats = useQuery({
    queryKey: ["team-routers", teamId, routerId, "stats"],
    queryFn: () => routerStats(teamId, routerId),
  });
  const savings = useQuery({
    queryKey: ["team-routers", teamId, routerId, "savings"],
    queryFn: () => routerSavings(teamId, routerId),
  });
  const decisions = useQuery({
    queryKey: ["team-routers", teamId, routerId, "decisions", offset],
    queryFn: () => listDecisionsPage(teamId, routerId, offset),
  });

  useEffect(() => setOffset(0), [routerId]);
  useEffect(() => {
    if (!decisions.isFetching && decisions.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [decisions.data, decisions.isFetching, offset]);

  return (
    <>
      <PageHeader
        command={`routing inspect ${router?.name ?? routerId.slice(0, 8)}`}
        title={router?.name ?? "Router"}
        description={
          router
            ? `strategy ${router.strategy}${router.shadow_strategy ? ` · shadow ${router.shadow_strategy}` : ""} · ${router.candidates.length} candidates · default ${router.default_model}`
            : `id ${routerId}`
        }
        actions={
          <>
            <Button variant="outline" size="sm" asChild>
              <a href={decisionsExportHref(teamId, routerId)} download>
                <Download className="h-4 w-4" />
                export jsonl
              </a>
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link to="/routing">
                <ArrowLeft className="h-4 w-4" />
                routing
              </Link>
            </Button>
          </>
        }
      />

      <div className="mb-6 grid gap-3 sm:grid-cols-3">
        <Stat label="decisions" value={stats.data?.total ?? "—"} />
        <Stat
          label="estimated savings"
          value={savings.data ? formatUsd(savings.data.estimated_savings) : "—"}
        />
        <Stat label="decisions costed" value={savings.data?.decisions_counted ?? "—"} />
      </div>

      <div className="mb-6 grid gap-3 lg:grid-cols-2">
        {stats.data ? <Distribution label="by model" entries={stats.data.by_model} /> : null}
        {stats.data ? <Distribution label="by tier" entries={stats.data.by_tier} /> : null}
        {stats.data && Object.keys(stats.data.shadow_by_model).length > 0 ? (
          <Distribution label="shadow · by model" entries={stats.data.shadow_by_model} />
        ) : null}
      </div>

      <p className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // recent decisions
      </p>
      <DataTable
        columns={DECISION_COLUMNS}
        rows={decisions.data?.items}
        rowKey={(d) => d.id}
        isLoading={decisions.isLoading}
        error={decisions.error as Error | null}
        emptyTitle="no decisions yet"
        emptyDescription="Decisions appear once clients start calling this router."
      />
      {decisions.data ? (
        <PaginationControls
          offset={decisions.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={decisions.data.items.length}
          hasNext={decisions.data.hasNext}
          isFetching={decisions.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}
    </>
  );
}
