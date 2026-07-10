import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { PageHeader } from "@/components/common/PageHeader";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { listOrganizations } from "@/features/organizations/api";
import { listTeams, type Team } from "@/features/teams/api";

function formatDate(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

export function TeamsPage() {
  const teams = useQuery({ queryKey: ["teams"], queryFn: listTeams });
  // Teams carry only organization_id; resolve names from the orgs list (cached).
  const orgs = useQuery({ queryKey: ["organizations"], queryFn: listOrganizations });
  const orgName = new Map((orgs.data ?? []).map((o) => [o.id, o.name]));

  const columns: Column<Team>[] = [
    { key: "status", header: "", className: "w-8", cell: () => <StatusDot tone="green" /> },
    {
      key: "name",
      header: "name",
      cell: (t) => (
        <Link
          to="/teams/$teamId"
          params={{ teamId: t.id }}
          className="font-medium text-foreground hover:text-primary hover:underline"
        >
          {t.name}
        </Link>
      ),
    },
    {
      key: "organization",
      header: "organization",
      cell: (t) => (
        <Link
          to="/organizations/$organizationId"
          params={{ organizationId: t.organization_id }}
          className="text-sm text-muted-foreground hover:text-primary hover:underline"
        >
          {orgName.get(t.organization_id) ?? t.organization_id.slice(0, 8)}
        </Link>
      ),
    },
    {
      key: "created_at",
      header: "created",
      cell: (t) => (
        <span className="text-xs text-muted-foreground">{formatDate(t.created_at)}</span>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        command="teams list"
        title="Teams"
        description="Every team across all organizations. Members, budget, and usage live on each team."
        actions={<Badge variant="muted">{teams.data ? `${teams.data.length} total` : "…"}</Badge>}
      />
      <DataTable
        columns={columns}
        rows={teams.data}
        rowKey={(t) => t.id}
        isLoading={teams.isLoading}
        error={teams.error as Error | null}
        emptyTitle="no teams"
        emptyDescription="Create a team from an organization's detail page."
      />
    </>
  );
}
