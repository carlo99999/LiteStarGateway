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
import { parseTags, updateTeam, type Team } from "@/features/teams/api";

interface TeamEditDialogProps {
  /** The team being edited, or null when the dialog is closed. */
  team: Team | null;
  onOpenChange: (open: boolean) => void;
}

/** Edit a team's name, description, and tags. */
export function TeamEditDialog({ team, onOpenChange }: TeamEditDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tagsText, setTagsText] = useState("");

  useEffect(() => {
    if (team) {
      setName(team.name);
      setDescription(team.description ?? "");
      setTagsText(team.tags.join(", "));
    }
  }, [team]);

  const mutation = useMutation({
    mutationFn: () =>
      updateTeam(team!.id, {
        name: name.trim(),
        description: description.trim() || null,
        tags: parseTags(tagsText),
      }),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: ["teams"] });
      await queryClient.invalidateQueries({ queryKey: ["teams", saved.id] });
      onOpenChange(false);
    },
  });

  const canSubmit = name.trim().length > 0 && !mutation.isPending;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
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
          <DialogTitle>Edit team</DialogTitle>
          <DialogDescription>
            Update the name, description, and tags. Members are unaffected.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="team-edit-name">name</Label>
            <Input
              id="team-edit-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoFocus
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="team-edit-description">description</Label>
            <Input
              id="team-edit-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="optional"
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="team-edit-tags">tags</Label>
            <Input
              id="team-edit-tags"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
              placeholder="comma, separated, labels"
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
