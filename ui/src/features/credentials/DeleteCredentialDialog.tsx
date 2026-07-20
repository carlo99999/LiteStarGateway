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
import { deleteCredential, type Credential } from "@/features/credentials/api";

interface DeleteCredentialDialogProps {
  /** The credential pending deletion, or null when the dialog is closed. */
  credential: Credential | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm deleting a credential. The backend refuses (409) while any model
 * still references it — that message is surfaced here. */
export function DeleteCredentialDialog({ credential, onOpenChange }: DeleteCredentialDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (c: Credential) => deleteCredential(c.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["credentials"] });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={credential !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete credential</DialogTitle>
          <DialogDescription>
            Permanently delete <span className="font-mono text-foreground">{credential?.name}</span>
            ? Its encrypted values are removed. If any model still uses it, deletion is refused —
            repoint or remove those models first.
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
            disabled={mutation.isPending || credential === null}
            onClick={() => credential && mutation.mutate(credential)}
          >
            {mutation.isPending ? "deleting…" : "delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
