import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { CreateServicePrincipalDialog } from "@/features/service-principals/CreateServicePrincipalDialog";
import { DeleteServicePrincipalDialog } from "@/features/service-principals/DeleteServicePrincipalDialog";
import {
  ServicePrincipalsTable,
  type ServicePrincipalRow,
} from "@/features/service-principals/ServicePrincipalsTable";
import {
  listServicePrincipalsPage,
  type ServicePrincipal,
} from "@/features/service-principals/api";
import { listAllTeams } from "@/features/teams/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Global service-principals view: pick a team, then manage its identities. */
export function ServicePrincipalsPage() {
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const [teamId, setTeamId] = useState("");
  const [offset, setOffset] = useState(0);
  const selectedTeam = teams.data?.find((team) => team.id === teamId);
  const sps = useQuery({
    queryKey: ["team-service-principals", teamId, "page", offset],
    queryFn: () => listServicePrincipalsPage(teamId, offset),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  useEffect(() => setOffset(0), [teamId]);
  useEffect(() => {
    if (!sps.isFetching && sps.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [sps.data, sps.isFetching, offset]);

  const rows: ServicePrincipalRow[] | undefined = sps.data?.items.map((sp) => ({
    ...sp,
    teamName: selectedTeam?.name,
  }));
  const error = (teams.error ?? sps.error ?? null) as Error | null;

  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<ServicePrincipal | null>(null);

  return (
    <>
      <PageHeader
        command="service-principals list"
        title="Service Principals"
        description="Team-owned identities that can hold management-scoped keys. Select a team to manage its principals."
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            New service principal
          </Button>
        }
      />
      <div className="mb-3 flex items-center gap-3">
        <Label htmlFor="sp-team-filter">team</Label>
        <select
          id="sp-team-filter"
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
      <ServicePrincipalsTable
        rows={rows}
        isLoading={teams.isLoading || (teamId.length > 0 && sps.isLoading)}
        error={error}
        showTeam
        onDelete={setDeleting}
        emptyDescription={teamId ? "Create one with the button above." : "Select a team."}
      />
      {sps.data ? (
        <PaginationControls
          offset={sps.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={sps.data.items.length}
          hasNext={sps.data.hasNext}
          isFetching={sps.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}

      <CreateServicePrincipalDialog open={createOpen} onOpenChange={setCreateOpen} />
      <DeleteServicePrincipalDialog
        sp={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </>
  );
}
