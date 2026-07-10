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
import { deleteOrganization, type Organization } from "@/features/organizations/api";

interface DeleteOrganizationDialogProps {
  /** The organization pending deletion, or null when the dialog is closed. */
  organization: Organization | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm deletion of an organization. The API refuses (409) if it still has
 * teams — that message is surfaced inline rather than pre-checked here. */
export function DeleteOrganizationDialog({
  organization,
  onOpenChange,
}: DeleteOrganizationDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (id: string) => deleteOrganization(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["organizations"] });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={organization !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete organization</DialogTitle>
          <DialogDescription>
            Delete{" "}
            <span className="font-mono text-foreground">{organization?.name}</span>? This cannot be
            undone. An organization that still has teams can't be deleted.
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
            disabled={mutation.isPending || organization === null}
            onClick={() => organization && mutation.mutate(organization.id)}
          >
            {mutation.isPending ? "deleting…" : "delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
