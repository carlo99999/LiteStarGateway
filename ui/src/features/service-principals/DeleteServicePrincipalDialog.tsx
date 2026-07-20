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
import { deleteServicePrincipal, type ServicePrincipal } from "@/features/service-principals/api";

interface DeleteServicePrincipalDialogProps {
  /** The service principal pending deletion, or null when the dialog is closed. */
  sp: ServicePrincipal | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm deleting a service principal. Deletion also revokes all of its keys,
 * so any client using them stops working immediately. */
export function DeleteServicePrincipalDialog({ sp, onOpenChange }: DeleteServicePrincipalDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (target: ServicePrincipal) => deleteServicePrincipal(target.team_id, target.id),
    onSuccess: async (_data, target) => {
      await queryClient.invalidateQueries({
        queryKey: ["team-service-principals", target.team_id],
      });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={sp !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete service principal</DialogTitle>
          <DialogDescription>
            Delete <span className="font-mono text-foreground">{sp?.name}</span>? All of its API
            keys are revoked immediately, and any client using them stops working. This can&apos;t
            be undone.
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
            disabled={mutation.isPending || sp === null}
            onClick={() => sp && mutation.mutate(sp)}
          >
            {mutation.isPending ? "deleting…" : "delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
