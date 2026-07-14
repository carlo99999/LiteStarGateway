import { useQuery } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/features/auth/use-auth";
import { DeleteUserDialog } from "@/features/users/DeleteUserDialog";
import { InviteUserDialog } from "@/features/users/InviteUserDialog";
import { UsersTable } from "@/features/users/UsersTable";
import { listUsers, type User } from "@/features/users/api";

/** Platform users: list, toggle roles (admin/auditor), enable/disable, unlock,
 * delete, and invite. New accounts are created by invite — pick the team and
 * role, then hand the invitee the one-time token to set their password. */
export function UsersPage() {
  const { user } = useAuth();
  const users = useQuery({ queryKey: ["users"], queryFn: listUsers });

  const [inviteOpen, setInviteOpen] = useState(false);
  const [deleting, setDeleting] = useState<User | null>(null);

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
        rows={users.isLoading ? undefined : users.data}
        isLoading={users.isLoading}
        error={(users.error ?? null) as Error | null}
        currentUserId={user?.id}
        onDelete={setDeleting}
      />

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
