import { Link } from "@tanstack/react-router";
import { RotateCw, Trash2 } from "lucide-react";
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
  /** Service-principal id → display name, so SP keys show the principal's name
   * instead of an opaque id. Personal keys are unaffected. */
  servicePrincipalNames?: Map<string, string>;
  onRotate: (key: ApiKey) => void;
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
  servicePrincipalNames,
  onRotate,
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
    {
      key: "name",
      header: "name",
      cell: (k) => (
        <span className="flex flex-col">
          <span className="text-foreground">{k.name ?? "—"}</span>
          {k.is_active && k.revoked_at ? (
            <span className="text-[10px] text-[color:var(--warning)]">
              grace until {formatWhen(k.revoked_at)}
            </span>
          ) : null}
          {k.is_active && !k.revoked_at && k.expires_at ? (
            <span className="text-[10px] text-muted-foreground">
              expires {formatWhen(k.expires_at)}
            </span>
          ) : null}
        </span>
      ),
    },
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
      key: "owner",
      header: "owner",
      cell: (k) => {
        // Service-principal keys act as a team identity; personal keys are
        // attributed to the user who created them.
        if (k.service_principal_id) {
          const name =
            servicePrincipalNames?.get(k.service_principal_id) ??
            `${k.service_principal_id.slice(0, 8)}…`;
          return (
            <span className="flex items-center gap-1.5">
              <Badge variant="accent">sp</Badge>
              <span className="text-xs text-muted-foreground">{name}</span>
            </span>
          );
        }
        return k.created_by === currentUserId ? (
          <Badge variant="muted">me</Badge>
        ) : (
          <span className="tabular text-xs text-muted-foreground">{k.created_by.slice(0, 8)}…</span>
        );
      },
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
      className: "w-20",
      numeric: true,
      cell: (k) =>
        k.is_active ? (
          <span className="flex justify-end gap-1">
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              aria-label={`Rotate ${k.name ?? k.prefix}`}
              onClick={() => onRotate(k)}
            >
              <RotateCw className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7 text-muted-foreground hover:text-destructive"
              aria-label={`Revoke ${k.name ?? k.prefix}`}
              onClick={() => onRevoke(k)}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </span>
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
