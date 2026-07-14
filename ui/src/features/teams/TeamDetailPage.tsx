import { useQuery } from "@tanstack/react-query";
import { getRouteApi, Link } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  getTeam,
  getTeamBudget,
  listTeamMembers,
  listTeamUsage,
  type TeamMember,
} from "@/features/teams/api";

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

  const team = useQuery({ queryKey: ["teams", teamId], queryFn: () => getTeam(teamId) });
  const members = useQuery({
    queryKey: ["teams", teamId, "members"],
    queryFn: () => listTeamMembers(teamId),
  });
  const budget = useQuery({
    queryKey: ["teams", teamId, "budget"],
    queryFn: () => getTeamBudget(teamId),
  });
  const usage = useQuery({
    queryKey: ["teams", teamId, "usage"],
    queryFn: () => listTeamUsage(teamId),
  });

  const usageTotal = (usage.data ?? []).reduce((sum, u) => sum + u.cost, 0);

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
        <Stat label="members" value={members.isLoading ? "…" : (members.data?.length ?? "—")} />
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
              : budget.data
                ? `${formatUsd(budget.data.limit_cost)} / ${budget.data.window}`
                : "none"
          }
        />
        <Stat label="usage · all-time" value={usage.isLoading ? "…" : formatUsd(usageTotal)} />
      </div>

      <p className="mb-2 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // members
      </p>
      <DataTable
        columns={memberColumns}
        rows={members.data}
        rowKey={(m) => m.id}
        isLoading={members.isLoading}
        error={members.error as Error | null}
        emptyTitle="no members"
        emptyDescription="This team has no members."
      />
    </>
  );
}
