import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { CreateRouterDialog } from "@/features/routing/CreateRouterDialog";
import { DeleteRouterDialog } from "@/features/routing/DeleteRouterDialog";
import { RoutersTable } from "@/features/routing/RoutersTable";
import { listRouters, type Router } from "@/features/routing/api";
import { listAllTeams } from "@/features/teams/api";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Smart routers (virtual models): pick a team, then manage its routers. Each
 * router's name links to its detail page (stats, savings, decisions). */
export function RoutingPage() {
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const [teamId, setTeamId] = useState("");
  const routers = useQuery({
    queryKey: ["team-routers", teamId],
    queryFn: () => listRouters(teamId),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);

  const error = (teams.error ?? routers.error ?? null) as Error | null;

  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<Router | null>(null);

  return (
    <>
      <PageHeader
        command="routing inspect"
        title="Routing"
        description="Smart routers — virtual models that pick the best candidate per request. Select a team to manage its routers."
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)} disabled={!teamId}>
            <Plus className="h-4 w-4" />
            New router
          </Button>
        }
      />
      <div className="mb-3 flex items-center gap-3">
        <Label htmlFor="routing-team">team</Label>
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
      <RoutersTable
        teamId={teamId}
        rows={routers.data}
        isLoading={teams.isLoading || (teamId.length > 0 && routers.isLoading)}
        error={error}
        onDelete={setDeleting}
      />

      <CreateRouterDialog teamId={teamId} open={createOpen} onOpenChange={setCreateOpen} />
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
