import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { updateTeam, type Team } from "@/features/teams/api";

interface TeamRenameDialogProps {
  /** The team being renamed, or null when the dialog is closed. */
  team: Team | null;
  onOpenChange: (open: boolean) => void;
}

/** Rename a team. */
export function TeamRenameDialog({ team, onOpenChange }: TeamRenameDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");

  useEffect(() => {
    if (team) setName(team.name);
  }, [team]);

  const mutation = useMutation({
    mutationFn: (value: string) => updateTeam(team!.id, value),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: ["teams"] });
      await queryClient.invalidateQueries({ queryKey: ["teams", saved.id] });
      onOpenChange(false);
    },
  });

  const trimmed = name.trim();
  const canSubmit = trimmed.length > 0 && trimmed !== team?.name && !mutation.isPending;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate(trimmed);
  }

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
          <DialogTitle>Rename team</DialogTitle>
          <DialogDescription>Change the team&apos;s display name.</DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="team-rename">name</Label>
            <Input
              id="team-rename"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              autoComplete="off"
            />
          </div>
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
            <Button type="submit" disabled={!canSubmit}>
              {mutation.isPending ? "saving…" : "save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
