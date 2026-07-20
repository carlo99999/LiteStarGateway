import { useQuery } from "@tanstack/react-query";
import { getRouteApi, Link } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TeamKeys } from "@/features/api-keys/TeamKeys";
import { TeamModels } from "@/features/models/TeamModels";
import { TeamServicePrincipals } from "@/features/service-principals/TeamServicePrincipals";
import {
  getTeam,
  getTeamBudget,
  listAllTeamUsage,
  listTeamMembersPage,
  type TeamMember,
} from "@/features/teams/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";

const route = getRouteApi("/app/teams/$teamId");

function formatUsd(cost: number): string {
  return `$${cost.toFixed(cost !== 0 && Math.abs(cost) < 0.01 ? 5 : 2)}`;
}

const memberColumns: Column<TeamMember>[] = [
  {
    key: "role",
    header: "role",
    cell: (m) => <Badge variant="muted">{m.role}</Badge>,
  },
  {
    key: "user_id",
    header: "user",
    cell: (m) => <span className="tabular text-xs text-muted-foreground">{m.user_id}</span>,
  },
];

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

export function TeamDetailPage() {
  const { teamId } = route.useParams();
  const [memberOffset, setMemberOffset] = useState(0);

  const team = useQuery({ queryKey: ["teams", teamId], queryFn: () => getTeam(teamId) });
  const members = useQuery({
    queryKey: ["teams", teamId, "members", "page", memberOffset],
    queryFn: () => listTeamMembersPage(teamId, memberOffset),
  });
  const budget = useQuery({
    queryKey: ["teams", teamId, "budget"],
    queryFn: () => getTeamBudget(teamId),
  });
  const usage = useQuery({
    queryKey: ["teams", teamId, "usage", "all"],
    queryFn: ({ signal }) => listAllTeamUsage(teamId, signal),
    retry: false,
  });

  useEffect(() => setMemberOffset(0), [teamId]);
  useEffect(() => {
    if (!members.isFetching && members.data?.items.length === 0 && memberOffset > 0) {
      setMemberOffset(previousPageOffset(memberOffset));
    }
  }, [memberOffset, members.data, members.isFetching]);

  const usageTotal = (usage.data ?? []).reduce((sum, u) => sum + u.cost, 0);
  const memberCount = members.data
    ? `${members.data.offset + members.data.items.length}${members.data.hasNext ? "+" : ""}`
    : "—";

  return (
    <>
      <PageHeader
        command={`teams get ${teamId.slice(0, 8)}`}
        title={team.data?.name ?? "Team"}
        description={`id ${teamId}`}
        actions={
          <Button variant="ghost" size="sm" asChild>
            <Link to="/teams">
              <ArrowLeft className="h-4 w-4" />
              teams
            </Link>
          </Button>
        }
      />

      {team.data ? (
        <p className="mb-4 font-mono text-xs text-muted-foreground">
          organization{" "}
          <Link
            to="/organizations/$organizationId"
            params={{ organizationId: team.data.organization_id }}
            className="text-primary hover:underline"
          >
            {team.data.organization_id}
          </Link>
        </p>
      ) : null}

      {team.data?.description ? (
        <p className="mb-3 max-w-2xl text-sm text-muted-foreground">{team.data.description}</p>
      ) : null}
      {team.data?.tags.length ? (
        <div className="mb-6 flex flex-wrap gap-1">
          {team.data.tags.map((tag) => (
            <Badge key={tag} variant="muted">
              {tag}
            </Badge>
          ))}
        </div>
      ) : null}

      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="members" value={members.isLoading ? "…" : memberCount} />
        <Stat
          label="rate limit"
          value={
            team.isLoading
              ? "…"
              : team.data?.rate_limit_rpm
                ? `${team.data.rate_limit_rpm}/min`
                : "unlimited"
          }
        />
        <Stat
          label="budget"
          value={
            budget.isLoading
              ? "…"
              : budget.isError
                ? "unavailable"
              : budget.data
                ? `${formatUsd(budget.data.limit_cost)} / ${budget.data.window}`
                : "none"
          }
        />
        <Stat
          label="usage · all-time"
          value={usage.isLoading ? "…" : usage.isError ? "unavailable" : formatUsd(usageTotal)}
        />
      </div>

      {budget.isError ? (
        <p role="alert" className="mb-6 font-mono text-xs text-destructive">
          ! {budget.error instanceof Error ? budget.error.message : "Failed to load budget"}
        </p>
      ) : null}
      {usage.isError ? (
        <p role="alert" className="mb-6 font-mono text-xs text-destructive">
          ! {usage.error instanceof Error ? usage.error.message : "Failed to load usage"}
        </p>
      ) : null}

      <p className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // members
      </p>
      <DataTable
        columns={memberColumns}
        rows={members.data?.items}
        rowKey={(m) => m.id}
        isLoading={members.isLoading}
        error={members.error as Error | null}
        emptyTitle="no members"
        emptyDescription="This team has no members."
      />
      {members.data ? (
        <PaginationControls
          offset={members.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={members.data.items.length}
          hasNext={members.data.hasNext}
          isFetching={members.isFetching}
          onOffsetChange={setMemberOffset}
        />
      ) : null}

      <div className="mt-8">
        <TeamModels teamId={teamId} />
      </div>

      <div className="mt-8">
        <TeamKeys teamId={teamId} />
      </div>

      <div className="mt-8">
        <TeamServicePrincipals teamId={teamId} />
      </div>
    </>
  );
}
