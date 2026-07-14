import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { deleteUser, type User } from "@/features/users/api";

interface DeleteUserDialogProps {
  /** The user pending deletion, or null when the dialog is closed. */
  user: User | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm hard-deleting a user. The backend refuses (409) while the account
 * still has team memberships or API keys it created — that message is surfaced
 * here so the admin knows to remove those (or just deactivate) first. */
export function DeleteUserDialog({ user, onOpenChange }: DeleteUserDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (u: User) => deleteUser(u.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["users"] });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={user !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete user</DialogTitle>
          <DialogDescription>
            Permanently delete <span className="font-mono text-foreground">{user?.email}</span>?
            This can&apos;t be undone. If the account still belongs to a team or owns API keys,
            deletion is refused — deactivate it instead.
          </DialogDescription>
        </DialogHeader>
        {mutation.isError ? (
          <p className="font-mono text-xs text-destructive">{mutation.error.message}</p>
        ) : null}
        <DialogFooter>
          <Button
            type="button"
            variant="ghost"
            onClick={() => onOpenChange(false)}
            disabled={mutation.isPending}
          >
            cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={mutation.isPending || user === null}
            onClick={() => user && mutation.mutate(user)}
          >
            {mutation.isPending ? "deleting…" : "delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
