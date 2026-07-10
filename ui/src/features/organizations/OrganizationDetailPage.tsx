import { useQuery } from "@tanstack/react-query";
import { getRouteApi, Link } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  getOrganization,
  getOrganizationSpend,
  type OrganizationSpend,
} from "@/features/organizations/api";

// The route lives under the pathless "app" layout route, so its id is prefixed
// with /app (the URL stays /organizations/$organizationId).
const route = getRouteApi("/app/organizations/$organizationId");

const SPEND_WINDOW_DAYS = 30;

function formatUsd(cost: number): string {
  // Model costs are often fractions of a cent; keep enough precision to be useful.
  return `$${cost.toFixed(cost !== 0 && Math.abs(cost) < 0.01 ? 5 : 2)}`;
}

type TeamSpend = OrganizationSpend["teams"][number];

const teamColumns: Column<TeamSpend>[] = [
  {
    key: "name",
    header: "team",
    cell: (t) => <span className="font-medium text-foreground">{t.name}</span>,
  },
  {
    key: "team_id",
    header: "id",
    cell: (t) => <span className="tabular text-xs text-muted-foreground">{t.team_id}</span>,
  },
  {
    key: "cost",
    header: `spend (${SPEND_WINDOW_DAYS}d)`,
    numeric: true,
    cell: (t) => <span className="tabular text-foreground">{formatUsd(t.cost)}</span>,
  },
];

export function OrganizationDetailPage() {
  const { organizationId } = route.useParams();

  const org = useQuery({
    queryKey: ["organizations", organizationId],
    queryFn: () => getOrganization(organizationId),
  });
  const spend = useQuery({
    queryKey: ["organizations", organizationId, "spend", SPEND_WINDOW_DAYS],
    queryFn: () => getOrganizationSpend(organizationId, SPEND_WINDOW_DAYS),
  });

  return (
    <>
      <PageHeader
        command={`organizations get ${organizationId.slice(0, 8)}`}
        title={org.data?.name ?? "Organization"}
        description={`id ${organizationId}`}
        actions={
          <Button variant="ghost" size="sm" asChild>
            <Link to="/organizations">
              <ArrowLeft className="h-4 w-4" />
              organizations
            </Link>
          </Button>
        }
      />

      {org.data?.description ? (
        <p className="mb-3 max-w-2xl text-sm text-muted-foreground">{org.data.description}</p>
      ) : null}
      {org.data?.tags.length ? (
        <div className="mb-6 flex flex-wrap gap-1">
          {org.data.tags.map((tag) => (
            <Badge key={tag} variant="muted">
              {tag}
            </Badge>
          ))}
        </div>
      ) : null}

      <div className="mb-6 grid gap-4 sm:grid-cols-2">
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            // spend · last {SPEND_WINDOW_DAYS} days
          </p>
          <p className="tabular mt-2 text-3xl text-foreground">
            {spend.isLoading ? "…" : spend.data ? formatUsd(spend.data.total_cost) : "—"}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            // teams
          </p>
          <p className="tabular mt-2 text-3xl text-foreground">
            {spend.isLoading ? "…" : (spend.data?.teams.length ?? "—")}
          </p>
        </div>
      </div>

      <DataTable
        columns={teamColumns}
        rows={spend.data?.teams}
        rowKey={(t) => t.team_id}
        isLoading={spend.isLoading}
        error={spend.error as Error | null}
        emptyTitle="no teams"
        emptyDescription="This organization has no teams yet."
      />
    </>
  );
}
