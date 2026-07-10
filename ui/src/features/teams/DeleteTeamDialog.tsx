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
import { deleteTeam, type Team } from "@/features/teams/api";

interface DeleteTeamDialogProps {
  /** The team pending deletion, or null when the dialog is closed. */
  team: Team | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm deletion of a team. The API refuses (409) if it still has models or
 * API keys; that message is surfaced inline. Members, budget, routers, service
 * principals, and usage history are removed with the team. */
export function DeleteTeamDialog({ team, onOpenChange }: DeleteTeamDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (id: string) => deleteTeam(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["teams"] });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={team !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete team</DialogTitle>
          <DialogDescription>
            Delete <span className="font-mono text-foreground">{team?.name}</span>? Its members,
            budget, routers, service principals, and usage history go with it. A team that still
            has models or API keys can&apos;t be deleted.
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
            disabled={mutation.isPending || team === null}
            onClick={() => team && mutation.mutate(team.id)}
          >
            {mutation.isPending ? "deleting…" : "delete"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
