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
import { DeleteOrganizationDialog } from "@/features/organizations/DeleteOrganizationDialog";
import { OrganizationFormDialog } from "@/features/organizations/OrganizationFormDialog";
import { listOrganizationsPage, type Organization } from "@/features/organizations/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

function formatDate(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

export function OrganizationsPage() {
  const [offset, setOffset] = useState(0);
  const query = useQuery({
    queryKey: ["organizations", "page", offset],
    queryFn: () => listOrganizationsPage(offset),
  });

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Organization | null>(null);
  const [deleting, setDeleting] = useState<Organization | null>(null);

  useEffect(() => {
    if (!query.isFetching && query.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [offset, query.data, query.isFetching]);

  function openCreate() {
    setEditing(null);
    setFormOpen(true);
  }

  function openEdit(org: Organization) {
    setEditing(org);
    setFormOpen(true);
  }

  const columns: Column<Organization>[] = [
    { key: "status", header: "", className: "w-8", cell: () => <StatusDot tone="green" /> },
    {
      key: "name",
      header: "name",
      cell: (org) => (
        <span className="flex flex-col">
          <Link
            to="/organizations/$organizationId"
            params={{ organizationId: org.id }}
            className="font-medium text-foreground hover:text-primary hover:underline"
          >
            {org.name}
          </Link>
          {org.description ? (
            <span className="max-w-xs truncate text-xs text-muted-foreground">
              {org.description}
            </span>
          ) : null}
        </span>
      ),
    },
    {
      key: "id",
      header: "id",
      cell: (org) => <span className="tabular text-xs text-muted-foreground">{org.id}</span>,
    },
    {
      key: "created_at",
      header: "created",
      cell: (org) => (
        <span className="text-xs text-muted-foreground">{formatDate(org.created_at)}</span>
      ),
    },
    {
      key: "tags",
      header: "tags",
      cell: (org) =>
        org.tags.length ? (
          <span className="flex flex-wrap gap-1">
            {org.tags.map((tag) => (
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
      cell: (org) => (
        <span className="flex justify-end gap-1">
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7"
            aria-label={`Rename ${org.name}`}
            onClick={() => openEdit(org)}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground hover:text-destructive"
            aria-label={`Delete ${org.name}`}
            onClick={() => setDeleting(org)}
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
        command="organizations list"
        title="Organizations"
        description="Platform-scoped tenants. Create, rename, and delete — the API enforces every permission."
        actions={
          <Button size="sm" onClick={openCreate}>
            <Plus className="h-4 w-4" />
            New organization
          </Button>
        }
      />
      <DataTable
        columns={columns}
        rows={query.data?.items}
        rowKey={(org) => org.id}
        isLoading={query.isLoading}
        error={toError(query.error)}
        emptyTitle="no organizations"
        emptyDescription="Create your first tenant with the New organization button."
      />
      {query.data ? (
        <PaginationControls
          offset={query.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={query.data.items.length}
          hasNext={query.data.hasNext}
          isFetching={query.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}

      <OrganizationFormDialog
        mode={editing ? "edit" : "create"}
        organization={editing ?? undefined}
        open={formOpen}
        onOpenChange={setFormOpen}
      />
      <DeleteOrganizationDialog
        organization={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </>
  );
}
