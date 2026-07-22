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
import { setRouterEnabled, type CallableRouter, type Router } from "@/features/routing/api";

interface CallableRoutersTableProps {
  teamId: string;
  rows: CallableRouter[] | undefined;
  isLoading: boolean;
  error: Error | null;
  /** Team id → name, to label an extended router with its source team. */
  teamNames: Map<string, string>;
  onDelete: (router: Router) => void;
  onExtend: (router: Router) => void;
}

/** Routers a team can call — its own (full actions + detail link), plus the
 * extended and global ones (read-only here, managed from their source). */
export function CallableRoutersTable({
  teamId,
  rows,
  isLoading,
  error,
  teamNames,
  onDelete,
  onExtend,
}: CallableRoutersTableProps) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: (router: Router) => setRouterEnabled(teamId, router, !router.enabled),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["team-routers", teamId] }),
  });

  function originBadge(c: CallableRouter) {
    if (c.origin === "own") return <Badge variant="muted">team</Badge>;
    const src = c.source_team_id ? teamNames.get(c.source_team_id) : undefined;
    if (c.origin === "global") {
      return <Badge variant="accent">{src ? `global · from ${src}` : "global"}</Badge>;
    }
    return <Badge variant="accent">extended · {src ?? "another team"}</Badge>;
  }

  const columns: Column<CallableRouter>[] = [
    {
      key: "status",
      header: "",
      className: "w-8",
      cell: (c) =>
        c.router.enabled ? (
          <StatusDot tone="green" />
        ) : (
          <span className="font-mono text-[9px] uppercase text-muted-foreground/60">off</span>
        ),
    },
    {
      key: "name",
      header: "name",
      cell: (c) =>
        c.origin === "own" ? (
          <Link
            to="/routing/$teamId/$routerId"
            params={{ teamId, routerId: c.router.id }}
            className="text-foreground hover:text-primary hover:underline"
          >
            {c.alias}
          </Link>
        ) : (
          <span className="text-foreground">{c.alias}</span>
        ),
    },
    { key: "strategy", header: "strategy", cell: (c) => <Badge variant="muted">{c.router.strategy}</Badge> },
    { key: "origin", header: "origin", cell: originBadge },
    {
      key: "candidates",
      header: "candidates",
      numeric: true,
      cell: (c) => (
        <span className="tabular text-xs text-muted-foreground">{c.router.candidates.length}</span>
      ),
    },
    {
      key: "default",
      header: "default",
      cell: (c) => <span className="text-xs text-muted-foreground">{c.router.default_model}</span>,
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (c) =>
        c.origin !== "own" ? (
          <span className="pr-2 text-[10px] text-muted-foreground/60">read-only</span>
        ) : (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label={`Actions for ${c.alias}`}
                disabled={toggle.isPending}
              >
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => onExtend(c.router)}>extend…</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => toggle.mutate(c.router)}>
                {c.router.enabled ? "disable" : "enable"}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem className="text-destructive" onSelect={() => onDelete(c.router)}>
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
      rowKey={(c) => `${c.origin}:${c.alias}`}
      isLoading={isLoading}
      error={error}
      emptyTitle="no routers"
      emptyDescription="This team can't call any router yet."
    />
  );
}
