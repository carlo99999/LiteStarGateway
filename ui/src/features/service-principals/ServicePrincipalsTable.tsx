import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { MoreHorizontal } from "lucide-react";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { setServicePrincipalEnabled, type ServicePrincipal } from "@/features/service-principals/api";

/** A row, optionally annotated with its team's display name. */
export type ServicePrincipalRow = ServicePrincipal & { teamName?: string };

interface ServicePrincipalsTableProps {
  rows: ServicePrincipalRow[] | undefined;
  isLoading: boolean;
  error: Error | null;
  /** Show a "team" column (the global view; implicit on a team page). */
  showTeam: boolean;
  onDelete: (sp: ServicePrincipal) => void;
  emptyDescription: string;
}

function formatWhen(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

export function ServicePrincipalsTable({
  rows,
  isLoading,
  error,
  showTeam,
  onDelete,
  emptyDescription,
}: ServicePrincipalsTableProps) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: (sp: ServicePrincipal) => setServicePrincipalEnabled(sp.team_id, sp.id, !sp.enabled),
    onSettled: (_data, _err, sp) =>
      queryClient.invalidateQueries({ queryKey: ["team-service-principals", sp.team_id] }),
  });

  const columns: Column<ServicePrincipalRow>[] = [
    {
      key: "status",
      header: "",
      className: "w-8",
      cell: (sp) =>
        sp.enabled ? (
          <StatusDot tone="green" />
        ) : (
          <span className="font-mono text-[9px] uppercase text-muted-foreground/60">off</span>
        ),
    },
    { key: "name", header: "name", cell: (sp) => <span className="text-foreground">{sp.name}</span> },
    ...(showTeam
      ? [
          {
            key: "team",
            header: "team",
            cell: (sp: ServicePrincipalRow) => (
              <Link
                to="/teams/$teamId"
                params={{ teamId: sp.team_id }}
                className="text-sm text-muted-foreground hover:text-primary hover:underline"
              >
                {sp.teamName ?? `${sp.team_id.slice(0, 8)}…`}
              </Link>
            ),
          } satisfies Column<ServicePrincipalRow>,
        ]
      : []),
    {
      key: "enabled",
      header: "status",
      cell: (sp) => (
        <span className="text-xs text-muted-foreground">{sp.enabled ? "enabled" : "disabled"}</span>
      ),
    },
    {
      key: "created",
      header: "created",
      cell: (sp) => (
        <span className="text-xs text-muted-foreground">{formatWhen(sp.created_at)}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (sp) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label={`Actions for ${sp.name}`}
              disabled={toggle.isPending}
            >
              <MoreHorizontal className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => toggle.mutate(sp)}>
              {sp.enabled ? "disable" : "enable"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="text-destructive" onSelect={() => onDelete(sp)}>
              delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(sp) => sp.id}
      isLoading={isLoading}
      error={error}
      emptyTitle="no service principals"
      emptyDescription={emptyDescription}
    />
  );
}
