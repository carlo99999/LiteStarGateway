import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Globe, Trash2 } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  extendRouter,
  listRouterGrants,
  makeRouterGlobal,
  unextendRouter,
  type Router,
} from "@/features/routing/api";
import { listAllTeams } from "@/features/teams/api";

interface ExtendRouterDialogProps {
  /** The team-owned router to extend; null closes the dialog. */
  router: Router | null;
  onOpenChange: (open: boolean) => void;
}

/** Share a team-owned router with other teams (a grant, not a copy), or promote
 * it to a global router. A global/extended router routes in the caller's
 * context, so its candidates should be models the target team can reach. */
export function ExtendRouterDialog({ router, onOpenChange }: ExtendRouterDialogProps) {
  const queryClient = useQueryClient();
  const [picked, setPicked] = useState<Set<string>>(new Set());

  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    enabled: router !== null,
  });
  const grants = useQuery({
    queryKey: ["router-grants", router?.id],
    queryFn: ({ signal }) => listRouterGrants(router!.id, signal),
    enabled: router !== null,
  });

  const grantedTeamIds = new Set((grants.data ?? []).map((g) => g.team_id));

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["router-grants", router?.id] }),
      queryClient.invalidateQueries({ queryKey: ["team-routers"] }),
      queryClient.invalidateQueries({ queryKey: ["global-routers"] }),
    ]);
  }

  const extend = useMutation({
    mutationFn: () => extendRouter(router!.id, [...picked]),
    onSuccess: async () => {
      setPicked(new Set());
      await refresh();
    },
  });
  const promote = useMutation({
    mutationFn: () => makeRouterGlobal(router!.id),
    onSuccess: async () => {
      await refresh();
      onOpenChange(false);
    },
  });
  const revoke = useMutation({
    mutationFn: (grantId: string) => unextendRouter(grantId),
    onSuccess: refresh,
  });

  const busy = extend.isPending || promote.isPending || revoke.isPending;
  const error = extend.error ?? promote.error ?? revoke.error;

  function toggle(teamId: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(teamId)) next.delete(teamId);
      else next.add(teamId);
      return next;
    });
  }

  const selectable = (teams.data ?? []).filter(
    (t) => t.id !== router?.team_id && !grantedTeamIds.has(t.id),
  );

  return (
    <Dialog
      open={router !== null}
      onOpenChange={(next) => {
        if (!next) {
          extend.reset();
          promote.reset();
          revoke.reset();
          setPicked(new Set());
        }
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Extend router</DialogTitle>
          <DialogDescription>
            {router ? (
              <>
                Share <span className="text-foreground">{router.name}</span> with other teams — it
                routes in each caller&apos;s context, so its candidates should be models they can
                reach (global models). Clashing names get a suffixed alias.
              </>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          {grants.data && grants.data.length > 0 ? (
            <div className="grid gap-1.5">
              <Label>extended to</Label>
              <ul className="grid gap-1">
                {grants.data.map((g) => {
                  const team = teams.data?.find((t) => t.id === g.team_id);
                  return (
                    <li
                      key={g.id}
                      className="flex items-center justify-between rounded-md border border-border px-2 py-1"
                    >
                      <span className="text-sm">
                        {team?.name ?? `${g.team_id.slice(0, 8)}…`}
                        <span className="ml-2 font-mono text-[10px] text-muted-foreground">
                          {g.alias}
                        </span>
                      </span>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-6 w-6 text-muted-foreground hover:text-destructive"
                        aria-label={`Revoke ${team?.name ?? g.team_id}`}
                        disabled={busy}
                        onClick={() => revoke.mutate(g.id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : null}

          <div className="grid gap-1.5">
            <Label>extend to</Label>
            {selectable.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                {teams.isLoading ? "loading teams…" : "no other teams available"}
              </p>
            ) : (
              <ul className="grid max-h-48 gap-1 overflow-y-auto">
                {selectable.map((t) => (
                  <li key={t.id}>
                    <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 hover:bg-muted">
                      <input
                        type="checkbox"
                        checked={picked.has(t.id)}
                        onChange={() => toggle(t.id)}
                        disabled={busy}
                      />
                      <span className="text-sm">{t.name}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {error ? <p className="font-mono text-xs text-destructive">{error.message}</p> : null}
        </div>

        <DialogFooter className="flex-col gap-2 sm:flex-row sm:justify-between">
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={() => promote.mutate()}
            title="Make available to every team, present and future"
          >
            <Globe className="h-4 w-4" />
            Make global
          </Button>
          <div className="flex gap-2">
            <Button type="button" variant="ghost" disabled={busy} onClick={() => onOpenChange(false)}>
              close
            </Button>
            <Button
              type="button"
              disabled={busy || picked.size === 0}
              onClick={() => extend.mutate()}
            >
              {extend.isPending ? "extending…" : `extend to ${picked.size || ""}`}
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
