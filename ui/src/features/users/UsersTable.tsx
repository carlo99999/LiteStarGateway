import { useMutation, useQueryClient } from "@tanstack/react-query";
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
import {
  setUserActive,
  setUserAdmin,
  setUserAuditor,
  unlockUser,
  type User,
} from "@/features/users/api";

interface UsersTableProps {
  rows: User[] | undefined;
  isLoading: boolean;
  error: Error | null;
  /** The signed-in admin's id, so their own row reads "me" and self-destructive
   * actions are disabled. */
  currentUserId: string | undefined;
  onDelete: (user: User) => void;
}

/** A user-mutating action, dispatched through a single mutation. */
type Action =
  | { kind: "admin"; user: User; value: boolean }
  | { kind: "auditor"; user: User; value: boolean }
  | { kind: "active"; user: User; value: boolean }
  | { kind: "unlock"; user: User };

function run(action: Action): Promise<unknown> {
  switch (action.kind) {
    case "admin":
      return setUserAdmin(action.user.id, action.value);
    case "auditor":
      return setUserAuditor(action.user.id, action.value);
    case "active":
      return setUserActive(action.user.id, action.value);
    case "unlock":
      return unlockUser(action.user.id);
  }
}

function formatWhen(iso: string): string {
  return new Date(iso).toISOString().slice(0, 19).replace("T", " ");
}

export function UsersTable({ rows, isLoading, error, currentUserId, onDelete }: UsersTableProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: run,
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });

  const columns: Column<User>[] = [
    {
      key: "status",
      header: "",
      className: "w-8",
      cell: (u) =>
        u.is_active ? (
          <StatusDot tone="green" />
        ) : (
          <span className="font-mono text-[9px] uppercase text-muted-foreground/60">off</span>
        ),
    },
    {
      key: "email",
      header: "email",
      cell: (u) => (
        <span className="flex items-center gap-2">
          <span className="text-foreground">{u.email}</span>
          {u.id === currentUserId ? <Badge variant="muted">me</Badge> : null}
        </span>
      ),
    },
    {
      key: "roles",
      header: "roles",
      cell: (u) => (
        <span className="flex gap-1">
          {u.is_admin ? <Badge>admin</Badge> : null}
          {u.is_auditor ? <Badge variant="accent">auditor</Badge> : null}
          {!u.is_admin && !u.is_auditor ? (
            <span className="text-xs text-muted-foreground">member</span>
          ) : null}
        </span>
      ),
    },
    {
      key: "active",
      header: "status",
      cell: (u) => (
        <span className="text-xs text-muted-foreground">{u.is_active ? "active" : "disabled"}</span>
      ),
    },
    {
      key: "created",
      header: "created",
      cell: (u) => <span className="text-xs text-muted-foreground">{formatWhen(u.created_at)}</span>,
    },
    {
      key: "actions",
      header: "",
      className: "w-12",
      numeric: true,
      cell: (u) => {
        const isSelf = u.id === currentUserId;
        return (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                aria-label={`Actions for ${u.email}`}
                disabled={mutation.isPending}
              >
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {/* Self admin changes are refused by the backend (lockout footgun). */}
              <DropdownMenuItem
                disabled={isSelf}
                onSelect={() => mutation.mutate({ kind: "admin", user: u, value: !u.is_admin })}
              >
                {u.is_admin ? "revoke admin" : "make admin"}
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => mutation.mutate({ kind: "auditor", user: u, value: !u.is_auditor })}
              >
                {u.is_auditor ? "revoke auditor" : "make auditor"}
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={isSelf && u.is_active}
                onSelect={() => mutation.mutate({ kind: "active", user: u, value: !u.is_active })}
              >
                {u.is_active ? "deactivate" : "activate"}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => mutation.mutate({ kind: "unlock", user: u })}>
                unlock
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                disabled={isSelf}
                className="text-destructive"
                onSelect={() => onDelete(u)}
              >
                delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        );
      },
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(u) => u.id}
      isLoading={isLoading}
      error={error}
      emptyTitle="no users"
      emptyDescription="Invite a user with the button above."
    />
  );
}
