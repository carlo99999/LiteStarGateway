import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { listAllOrganizations } from "@/features/organizations/api";
import { CreateTeamDialog } from "@/features/teams/CreateTeamDialog";
import { DeleteTeamDialog } from "@/features/teams/DeleteTeamDialog";
import { TeamEditDialog } from "@/features/teams/TeamEditDialog";
import { listTeamsPage, type Team } from "@/features/teams/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";

function formatDate(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

export function TeamsPage() {
  const [offset, setOffset] = useState(0);
  const teams = useQuery({
    queryKey: ["teams", "page", offset],
    queryFn: () => listTeamsPage(offset),
  });
  // Teams carry only organization_id; resolve names from the orgs list (cached).
  const orgs = useQuery({
    queryKey: ["organizations", "all"],
    queryFn: ({ signal }) => listAllOrganizations(signal),
    retry: false,
  });
  const orgName = new Map((orgs.data ?? []).map((o) => [o.id, o.name]));

  const [editing, setEditing] = useState<Team | null>(null);
  const [deleting, setDeleting] = useState<Team | null>(null);
  const [createOpen, setCreateOpen] = useState(false);

  useEffect(() => {
    if (!teams.isFetching && teams.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [offset, teams.data, teams.isFetching]);

  const columns: Column<Team>[] = [
    { key: "status", header: "", className: "w-8", cell: () => <StatusDot tone="green" /> },
    {
      key: "name",
      header: "name",
      cell: (t) => (
        <span className="flex flex-col">
          <Link
            to="/teams/$teamId"
            params={{ teamId: t.id }}
            className="font-medium text-foreground hover:text-primary hover:underline"
          >
            {t.name}
          </Link>
          {t.description ? (
            <span className="max-w-xs truncate text-xs text-muted-foreground">
              {t.description}
            </span>
          ) : null}
        </span>
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
      key: "tags",
      header: "tags",
      cell: (t) =>
        t.tags.length ? (
          <span className="flex flex-wrap gap-1">
            {t.tags.map((tag) => (
              <Badge key={tag} variant="muted">
                {tag}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="text-muted-foreground/40">—</span>
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
            aria-label={`Edit ${t.name}`}
            onClick={() => setEditing(t)}
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
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            New team
          </Button>
        }
      />
      {orgs.isError ? (
        <p role="alert" className="mb-3 font-mono text-xs text-[color:var(--warning)]">
          ! Organization names are unavailable; IDs are shown instead.
        </p>
      ) : null}
      <DataTable
        columns={columns}
        rows={teams.data?.items}
        rowKey={(t) => t.id}
        isLoading={teams.isLoading}
        error={teams.error as Error | null}
        emptyTitle="no teams"
        emptyDescription="Create a team from an organization's detail page."
      />
      {teams.data ? (
        <PaginationControls
          offset={teams.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={teams.data.items.length}
          hasNext={teams.data.hasNext}
          isFetching={teams.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}

      <CreateTeamDialog open={createOpen} onOpenChange={setCreateOpen} />
      <TeamEditDialog
        team={editing}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
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
