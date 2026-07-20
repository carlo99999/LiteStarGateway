import { useQuery } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/features/auth/use-auth";
import { DeleteUserDialog } from "@/features/users/DeleteUserDialog";
import { InviteUserDialog } from "@/features/users/InviteUserDialog";
import { UsersTable } from "@/features/users/UsersTable";
import { listUsersPage, type User } from "@/features/users/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

/** Platform users: list, toggle roles (admin/auditor), enable/disable, unlock,
 * delete, and invite. New accounts are created by invite — pick the team and
 * role, then hand the invitee the one-time token to set their password. */
export function UsersPage() {
  const { user } = useAuth();
  const [offset, setOffset] = useState(0);
  const users = useQuery({
    queryKey: ["users", "page", offset],
    queryFn: () => listUsersPage(offset),
  });

  const [inviteOpen, setInviteOpen] = useState(false);
  const [deleting, setDeleting] = useState<User | null>(null);

  useEffect(() => {
    if (!users.isFetching && users.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [offset, users.data, users.isFetching]);

  return (
    <>
      <PageHeader
        command="users list"
        title="Users"
        description="Every account on the platform. Invite a user (choosing their team and role), manage roles, or deactivate access."
        actions={
          <Button size="sm" onClick={() => setInviteOpen(true)}>
            <UserPlus className="h-4 w-4" />
            Invite user
          </Button>
        }
      />
      <UsersTable
        rows={users.isLoading ? undefined : users.data?.items}
        isLoading={users.isLoading}
        error={toError(users.error)}
        currentUserId={user?.id}
        onDelete={setDeleting}
      />
      {users.data ? (
        <PaginationControls
          offset={users.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={users.data.items.length}
          hasNext={users.data.hasNext}
          isFetching={users.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}

      <InviteUserDialog open={inviteOpen} onOpenChange={setInviteOpen} />
      <DeleteUserDialog
        user={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </>
  );
}
