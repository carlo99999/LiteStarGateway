import { useQuery } from "@tanstack/react-query";
import { Globe, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { CallableRoutersTable } from "@/features/routing/CallableRoutersTable";
import { CreateRouterDialog } from "@/features/routing/CreateRouterDialog";
import { DeleteRouterDialog } from "@/features/routing/DeleteRouterDialog";
import { ExtendRouterDialog } from "@/features/routing/ExtendRouterDialog";
import { RoutersTable } from "@/features/routing/RoutersTable";
import { listCallableRouters, listGlobalRouters, type Router } from "@/features/routing/api";
import { listAllTeams } from "@/features/teams/api";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

const SECTION_LABEL = "font-mono text-[10px] uppercase tracking-widest text-muted-foreground";

/** Smart routers: global (platform) routers callable by every team, plus a
 * per-team section showing what each team can call (own + extended + global). */
export function RoutingPage() {
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const globalRouters = useQuery({
    queryKey: ["global-routers"],
    queryFn: () => listGlobalRouters(),
    retry: false,
  });
  const teamNames = useMemo(
    () => new Map((teams.data ?? []).map((t) => [t.id, t.name])),
    [teams.data],
  );

  const [teamId, setTeamId] = useState("");
  const routers = useQuery({
    queryKey: ["team-routers", teamId, "callable"],
    queryFn: () => listCallableRouters(teamId),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);

  const [createTeamOpen, setCreateTeamOpen] = useState(false);
  const [createGlobalOpen, setCreateGlobalOpen] = useState(false);
  const [deleting, setDeleting] = useState<Router | null>(null);
  const [extending, setExtending] = useState<Router | null>(null);

  return (
    <>
      <PageHeader
        command="routing inspect"
        title="Routing"
        description="Smart routers — virtual models that pick the best candidate per request. Global routers are callable by every team; team routers can be extended to others."
        actions={
          <Button size="sm" onClick={() => setCreateGlobalOpen(true)}>
            <Globe className="h-4 w-4" />
            New global router
          </Button>
        }
      />

      <p className={`mb-2 ${SECTION_LABEL}`}>// global routers · every team</p>
      <RoutersTable
        global
        rows={globalRouters.data}
        isLoading={globalRouters.isLoading}
        error={toError(globalRouters.error)}
        onDelete={setDeleting}
      />

      <div className="mb-2 mt-8 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <p className={SECTION_LABEL}>// team routers</p>
          <Label htmlFor="routing-team" className="sr-only">
            team
          </Label>
          <select
            id="routing-team"
            className={SELECT_CLASS}
            value={teamId}
            onChange={(event) => setTeamId(event.target.value)}
            disabled={teams.isLoading || teams.isError}
          >
            <option value="" disabled>
              {teams.isLoading
                ? "loading…"
                : teams.isError
                  ? "failed to load teams"
                  : "select a team"}
            </option>
            {(teams.data ?? []).map((team) => (
              <option key={team.id} value={team.id}>
                {team.name}
              </option>
            ))}
          </select>
        </div>
        <Button size="sm" variant="outline" onClick={() => setCreateTeamOpen(true)} disabled={!teamId}>
          <Plus className="h-4 w-4" />
          New team router
        </Button>
      </div>
      <CallableRoutersTable
        teamId={teamId}
        rows={routers.data}
        isLoading={teams.isLoading || (teamId.length > 0 && routers.isLoading)}
        error={toError(teams.error ?? routers.error)}
        teamNames={teamNames}
        onDelete={setDeleting}
        onExtend={setExtending}
      />

      <CreateRouterDialog teamId={teamId} open={createTeamOpen} onOpenChange={setCreateTeamOpen} />
      <CreateRouterDialog global open={createGlobalOpen} onOpenChange={setCreateGlobalOpen} />
      <ExtendRouterDialog
        router={extending}
        onOpenChange={(open) => {
          if (!open) setExtending(null);
        }}
      />
      <DeleteRouterDialog
        teamId={teamId}
        router={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </>
  );
}
