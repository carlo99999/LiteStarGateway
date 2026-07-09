import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/common/PageHeader";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { listOrganizations, type Organization } from "@/features/organizations/api";

function formatDate(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

const columns: Column<Organization>[] = [
  {
    key: "status",
    header: "",
    className: "w-8",
    cell: () => <StatusDot tone="green" />,
  },
  {
    key: "name",
    header: "name",
    cell: (org) => <span className="font-medium text-foreground">{org.name}</span>,
  },
  {
    key: "id",
    header: "id",
    cell: (org) => <span className="tabular text-xs text-muted-foreground">{org.id}</span>,
  },
  {
    key: "created_at",
    header: "created",
    numeric: true,
    cell: (org) => <span className="text-xs text-muted-foreground">{formatDate(org.created_at)}</span>,
  },
];

/** Phase 0 read-only proof view: fetch via the typed client + TanStack Query. */
export function OrganizationsPage() {
  const query = useQuery({
    queryKey: ["organizations"],
    queryFn: listOrganizations,
  });

  return (
    <>
      <PageHeader
        command="organizations list"
        title="Organizations"
        description="Platform-scoped tenants. Read-only in Phase 0 — the API remains the source of truth for every permission."
        actions={
          <Badge variant="muted">
            {query.data ? `${query.data.length} total` : "…"}
          </Badge>
        }
      />
      <DataTable
        columns={columns}
        rows={query.data}
        rowKey={(org) => org.id}
        isLoading={query.isLoading}
        error={query.error as Error | null}
        emptyTitle="no organizations"
        emptyDescription="Create one via the API or a future admin flow."
      />
    </>
  );
}
