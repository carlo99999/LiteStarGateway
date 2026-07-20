import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { MoreHorizontal } from "lucide-react";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { setRouterEnabled, type Router } from "@/features/routing/api";

interface RoutersTableProps {
  teamId: string;
  rows: Router[] | undefined;
  isLoading: boolean;
  error: Error | null;
  onDelete: (router: Router) => void;
}

export function RoutersTable({ teamId, rows, isLoading, error, onDelete }: RoutersTableProps) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: (router: Router) => setRouterEnabled(teamId, router, !router.enabled),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["team-routers", teamId] }),
  });

  const columns: Column<Router>[] = [
    {
      key: "status",
      header: "",
      className: "w-8",
      cell: (r) =>
        r.enabled ? (
          <StatusDot tone="green" />
        ) : (
          <span className="font-mono text-[9px] uppercase text-muted-foreground/60">off</span>
        ),
    },
    {
      key: "name",
      header: "name",
      cell: (r) => (
        <Link
          to="/routing/$teamId/$routerId"
          params={{ teamId, routerId: r.id }}
          className="text-foreground hover:text-primary hover:underline"
        >
          {r.name}
        </Link>
      ),
    },
    {
      key: "strategy",
      header: "strategy",
      cell: (r) => (
        <span className="flex items-center gap-1.5">
          <Badge variant="muted">{r.strategy}</Badge>
          {r.shadow_strategy ? <Badge variant="accent">shadow: {r.shadow_strategy}</Badge> : null}
        </span>
      ),
    },
    {
      key: "candidates",
      header: "candidates",
      numeric: true,
      cell: (r) => (
        <span className="tabular text-xs text-muted-foreground">{r.candidates.length}</span>
      ),
    },
    {
      key: "default",
      header: "default",
      cell: (r) => <span className="text-xs text-muted-foreground">{r.default_model}</span>,
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (r) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label={`Actions for ${r.name}`}
              disabled={toggle.isPending}
            >
              <MoreHorizontal className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => toggle.mutate(r)}>
              {r.enabled ? "disable" : "enable"}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="text-destructive" onSelect={() => onDelete(r)}>
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
      rowKey={(r) => r.id}
      isLoading={isLoading}
      error={error}
      emptyTitle="no routers"
      emptyDescription="Create a router with the button above."
    />
  );
}
