import { Link } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";
import { StatusDot } from "@/components/common/StatusDot";
import { DataTable, type Column } from "@/components/common/DataTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ApiKey } from "@/features/api-keys/api";

/** A key row, optionally annotated with its team's display name. */
export type KeyRow = ApiKey & { teamName?: string };

interface KeysTableProps {
  rows: KeyRow[] | undefined;
  isLoading: boolean;
  error: Error | null;
  /** Show a "team" column (the global /api-keys view; implicit on a team page). */
  showTeam: boolean;
  /** The signed-in user's id, so their own keys read "me". */
  currentUserId: string | undefined;
  onRevoke: (key: ApiKey) => void;
  emptyDescription: string;
}

function formatWhen(iso: string | null): string {
  return iso ? new Date(iso).toISOString().slice(0, 19).replace("T", " ") : "never";
}

export function KeysTable({
  rows,
  isLoading,
  error,
  showTeam,
  currentUserId,
  onRevoke,
  emptyDescription,
}: KeysTableProps) {
  const columns: Column<KeyRow>[] = [
    {
      key: "status",
      header: "",
      className: "w-8",
      cell: (k) =>
        k.is_active ? (
          <StatusDot tone="green" />
        ) : (
          <span className="font-mono text-[9px] uppercase text-muted-foreground/60">rvkd</span>
        ),
    },
    { key: "name", header: "name", cell: (k) => <span className="text-foreground">{k.name ?? "—"}</span> },
    ...(showTeam
      ? [
          {
            key: "team",
            header: "team",
            cell: (k: KeyRow) => (
              <Link
                to="/teams/$teamId"
                params={{ teamId: k.team_id }}
                className="text-sm text-muted-foreground hover:text-primary hover:underline"
              >
                {k.teamName ?? `${k.team_id.slice(0, 8)}…`}
              </Link>
            ),
          } satisfies Column<KeyRow>,
        ]
      : []),
    {
      key: "user",
      header: "user",
      cell: (k) =>
        k.created_by === currentUserId ? (
          <Badge variant="muted">me</Badge>
        ) : (
          <span className="tabular text-xs text-muted-foreground">{k.created_by.slice(0, 8)}…</span>
        ),
    },
    {
      key: "prefix",
      header: "prefix",
      cell: (k) => <span className="tabular text-xs text-muted-foreground">{k.prefix}…</span>,
    },
    { key: "scope", header: "scope", cell: (k) => <Badge variant="muted">{k.scope}</Badge> },
    {
      key: "rate",
      header: "rate limit",
      numeric: true,
      cell: (k) => (
        <span className="tabular text-xs text-muted-foreground">
          {k.rate_limit_rpm ? `${k.rate_limit_rpm}/min` : "—"}
        </span>
      ),
    },
    {
      key: "last_used",
      header: "last used",
      cell: (k) => (
        <span className="text-xs text-muted-foreground">{formatWhen(k.last_used_at)}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (k) =>
        k.is_active ? (
          <Button
            variant="ghost"
            size="icon"
            className="h-7 w-7 text-muted-foreground hover:text-destructive"
            aria-label={`Revoke ${k.name ?? k.prefix}`}
            onClick={() => onRevoke(k)}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        ) : null,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(k) => k.id}
      isLoading={isLoading}
      error={error}
      emptyTitle="no api keys"
      emptyDescription={emptyDescription}
    />
  );
}
