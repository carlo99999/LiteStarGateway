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
import { createTeam } from "@/features/teams/api";

interface CreateTeamDialogProps {
  organizationId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Create a team within an organization. The admin email must be an existing
 * user, who becomes the team's first admin. */
export function CreateTeamDialog({ organizationId, open, onOpenChange }: CreateTeamDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");

  useEffect(() => {
    if (open) {
      setName("");
      setAdminEmail("");
    }
  }, [open]);

  const mutation = useMutation({
    mutationFn: () => createTeam(organizationId, name.trim(), adminEmail.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["organizations", organizationId] });
      await queryClient.invalidateQueries({ queryKey: ["teams"] });
      onOpenChange(false);
    },
  });

  const canSubmit = name.trim().length > 0 && adminEmail.trim().length > 0 && !mutation.isPending;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New team</DialogTitle>
          <DialogDescription>
            Create a team in this organization. The admin email must be an existing user — they
            become the team&apos;s first admin.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="team-name">name</Label>
            <Input
              id="team-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Platform"
              autoFocus
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="team-admin">admin email</Label>
            <Input
              id="team-admin"
              type="email"
              value={adminEmail}
              onChange={(e) => setAdminEmail(e.target.value)}
              placeholder="lead@example.com"
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
              {mutation.isPending ? "creating…" : "create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
