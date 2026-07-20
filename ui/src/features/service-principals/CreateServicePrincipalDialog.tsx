import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { listAllTeams } from "@/features/teams/api";
import { createServicePrincipal } from "@/features/service-principals/api";

interface CreateServicePrincipalDialogProps {
  /** Fixed team (team detail page). Omit to show a team picker (global page). */
  teamId?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Create a team service principal — a team-owned identity that can later hold
 * management/all-scoped keys (issued from the API Keys page). */
export function CreateServicePrincipalDialog({
  teamId,
  open,
  onOpenChange,
}: CreateServicePrincipalDialogProps) {
  const queryClient = useQueryClient();
  const [teamPick, setTeamPick] = useState("");
  const [name, setName] = useState("");

  useEffect(() => {
    if (open) {
      setTeamPick("");
      setName("");
    }
  }, [open]);

  const effectiveTeamId = teamId ?? teamPick;

  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    enabled: open && !teamId,
  });

  const mutation = useMutation({
    mutationFn: () => createServicePrincipal(effectiveTeamId, name.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["team-service-principals", effectiveTeamId],
      });
      onOpenChange(false);
    },
  });

  const canSubmit = !mutation.isPending && effectiveTeamId.length > 0 && name.trim().length > 0;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) mutation.reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create service principal</DialogTitle>
          <DialogDescription>
            A team-owned identity. It holds no key yet — issue one from the API Keys page, where a
            service principal (unlike a personal key) may take management scope.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          {!teamId ? (
            <div className="grid gap-2">
              <Label htmlFor="sp-team">team</Label>
              <select
                id="sp-team"
                className={SELECT_CLASS}
                value={teamPick}
                onChange={(e) => setTeamPick(e.target.value)}
              >
                <option value="" disabled>
                  {teams.isLoading ? "loading…" : "select a team"}
                </option>
                {(teams.data ?? []).map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>
          ) : null}
          <div className="grid gap-2">
            <Label htmlFor="sp-name">name</Label>
            <Input
              id="sp-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. ci-deployer"
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
