import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Pencil, Trash2 } from "lucide-react";
import { useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { listOrganizations } from "@/features/organizations/api";
import { DeleteTeamDialog } from "@/features/teams/DeleteTeamDialog";
import { TeamRenameDialog } from "@/features/teams/TeamRenameDialog";
import { listTeams, type Team } from "@/features/teams/api";

function formatDate(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

export function TeamsPage() {
  const teams = useQuery({ queryKey: ["teams"], queryFn: listTeams });
  // Teams carry only organization_id; resolve names from the orgs list (cached).
  const orgs = useQuery({ queryKey: ["organizations"], queryFn: listOrganizations });
  const orgName = new Map((orgs.data ?? []).map((o) => [o.id, o.name]));

  const [renaming, setRenaming] = useState<Team | null>(null);
  const [deleting, setDeleting] = useState<Team | null>(null);

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
    {
      key: "actions",
      header: "",
      className: "w-20",
      numeric: true,
      cell: (t) => (
        <span className="flex justify-end gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label={`Rename ${t.name}`}
            onClick={() => setRenaming(t)}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground hover:text-destructive"
            aria-label={`Delete ${t.name}`}
            onClick={() => setDeleting(t)}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </span>
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

      <TeamRenameDialog
        team={renaming}
        onOpenChange={(open) => {
          if (!open) setRenaming(null);
        }}
      />
      <DeleteTeamDialog
        team={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </>
  );
}
