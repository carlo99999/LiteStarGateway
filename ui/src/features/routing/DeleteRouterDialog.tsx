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
import { deleteRouter, type Router } from "@/features/routing/api";

interface DeleteRouterDialogProps {
  teamId: string;
  /** The router pending deletion, or null when the dialog is closed. */
  router: Router | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm deleting a router. Clients calling the virtual model name stop
 * resolving it immediately. */
export function DeleteRouterDialog({ teamId, router, onOpenChange }: DeleteRouterDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (r: Router) => deleteRouter(teamId, r.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["team-routers", teamId] });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={router !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete router</DialogTitle>
          <DialogDescription>
            Delete <span className="font-mono text-foreground">{router?.name}</span>? Clients
            calling this virtual model stop resolving it immediately. Its decision history is kept.
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
            disabled={mutation.isPending || router === null}
            onClick={() => router && mutation.mutate(router)}
          >
            {mutation.isPending ? "deleting…" : "delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
