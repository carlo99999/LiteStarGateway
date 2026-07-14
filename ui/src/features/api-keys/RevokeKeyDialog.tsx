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
import { revokeTeamKey, type ApiKey } from "@/features/api-keys/api";

interface RevokeKeyDialogProps {
  /** The key pending revocation, or null when the dialog is closed. Carries its
   * own team_id, so it works from both the team page and the global view. */
  apiKey: ApiKey | null;
  onOpenChange: (open: boolean) => void;
}

/** Confirm revoking an API key. Revocation is immediate and permanent. */
export function RevokeKeyDialog({ apiKey, onOpenChange }: RevokeKeyDialogProps) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (key: ApiKey) => revokeTeamKey(key.team_id, key.id),
    onSuccess: async (_data, key) => {
      await queryClient.invalidateQueries({ queryKey: ["team-keys", key.team_id] });
      onOpenChange(false);
    },
  });

  return (
    <Dialog
      open={apiKey !== null}
      onOpenChange={(open) => {
        if (!open) {
          mutation.reset();
          onOpenChange(false);
        }
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Revoke API key</DialogTitle>
          <DialogDescription>
            Revoke{" "}
            <span className="font-mono text-foreground">{apiKey?.name ?? apiKey?.prefix}</span>?
            Any client using it stops working immediately. This can&apos;t be undone.
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
            disabled={mutation.isPending || apiKey === null}
            onClick={() => apiKey && mutation.mutate(apiKey)}
          >
            {mutation.isPending ? "revoking…" : "revoke"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
