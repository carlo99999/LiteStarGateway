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
import { deleteGlobalModel, deleteModel, type Model } from "@/features/models/api";

interface DeleteModelDialogProps {
  /** The model pending deletion, or null when the dialog is closed. */
  model: Model | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm deleting a model. Routers that reference it may stop resolving, so
 * this is a deliberate action. */
export function DeleteModelDialog({ model, onOpenChange }: DeleteModelDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (m: Model) =>
      m.team_id === null ? deleteGlobalModel(m.id) : deleteModel(m.team_id, m.id),
    onSuccess: async (_data, m) => {
      await queryClient.invalidateQueries({ queryKey: ["team-models", m.team_id] });
      await queryClient.invalidateQueries({ queryKey: ["global-models"] });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={model !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete model</DialogTitle>
          <DialogDescription>
            Delete <span className="font-mono text-foreground">{model?.name}</span>? Clients calling
            this model, and routers that list it, stop resolving it. This can&apos;t be undone.
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
            disabled={mutation.isPending || model === null}
            onClick={() => model && mutation.mutate(model)}
          >
            {mutation.isPending ? "deleting…" : "delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
