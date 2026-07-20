import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Label } from "@/components/ui/label";
import { listAllTeams } from "@/features/teams/api";
import { listTeamUsagePage, type TeamUsage } from "@/features/usage/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

function formatUsd(cost: number): string {
  return `$${cost.toFixed(4)}`;
}

function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}

const COLUMNS: Column<TeamUsage>[] = [
  {
    key: "model",
    header: "model",
    cell: (u) => <span className="text-foreground">{u.model}</span>,
  },
  {
    key: "calls",
    header: "calls",
    numeric: true,
    cell: (u) => (
      <span className="tabular text-xs text-muted-foreground">{formatCount(u.calls)}</span>
    ),
  },
  {
    key: "prompt",
    header: "prompt tokens",
    numeric: true,
    cell: (u) => (
      <span className="tabular text-xs text-muted-foreground">{formatCount(u.prompt_tokens)}</span>
    ),
  },
  {
    key: "completion",
    header: "completion tokens",
    numeric: true,
    cell: (u) => (
      <span className="tabular text-xs text-muted-foreground">
        {formatCount(u.completion_tokens)}
      </span>
    ),
  },
  {
    key: "total",
    header: "total tokens",
    numeric: true,
    cell: (u) => (
      <span className="tabular text-xs text-muted-foreground">{formatCount(u.total_tokens)}</span>
    ),
  },
  {
    key: "cost",
    header: "cost",
    numeric: true,
    cell: (u) => <span className="tabular text-xs text-foreground">{formatUsd(u.cost)}</span>,
  },
];

/** Per-model usage and spend for one team at a time. */
export function UsagePage() {
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const [teamId, setTeamId] = useState("");
  const [offset, setOffset] = useState(0);
  const usage = useQuery({
    queryKey: ["teams", teamId, "usage", "page", offset],
    queryFn: () => listTeamUsagePage(teamId, offset),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  useEffect(() => setOffset(0), [teamId]);
  useEffect(() => {
    if (!usage.isFetching && usage.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [usage.data, usage.isFetching, offset]);

  const pageCost = (usage.data?.items ?? []).reduce((sum, u) => sum + u.cost, 0);
  const error = toError(teams.error ?? usage.error);

  return (
    <>
      <PageHeader
        command="usage report"
        title="Usage & Cost"
        description="Per-model token and spend totals. Select a team to inspect its usage."
      />
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Label htmlFor="usage-team">team</Label>
          <select
            id="usage-team"
            className={SELECT_CLASS}
            value={teamId}
            onChange={(event) => setTeamId(event.target.value)}
            disabled={teams.isLoading || teams.isError}
          >
            <option value="" disabled>
              {teams.isLoading
                ? "loading…"
                : teams.isError
                  ? "failed to load teams"
                  : "select a team"}
            </option>
            {(teams.data ?? []).map((team) => (
              <option key={team.id} value={team.id}>
                {team.name}
              </option>
            ))}
          </select>
        </div>
        {usage.data ? (
          <p className="font-mono text-xs text-muted-foreground">
            page total: <span className="text-foreground">{formatUsd(pageCost)}</span>
          </p>
        ) : null}
      </div>
      <DataTable
        columns={COLUMNS}
        rows={usage.data?.items}
        rowKey={(u) => u.model}
        isLoading={teams.isLoading || (teamId.length > 0 && usage.isLoading)}
        error={error}
        emptyTitle="no usage"
        emptyDescription="Usage appears once this team's keys start calling the gateway."
      />
      {usage.data ? (
        <PaginationControls
          offset={usage.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={usage.data.items.length}
          hasNext={usage.data.hasNext}
          isFetching={usage.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}
    </>
  );
}
