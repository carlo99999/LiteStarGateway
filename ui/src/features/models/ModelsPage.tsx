import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useCredentialNames } from "@/features/credentials/useCredentialNames";
import { CreateModelDialog } from "@/features/models/CreateModelDialog";
import { DeleteModelDialog } from "@/features/models/DeleteModelDialog";
import { EditModelDialog } from "@/features/models/EditModelDialog";
import { ModelsTable } from "@/features/models/ModelsTable";
import { listModelsPage, type Model } from "@/features/models/api";
import { listAllTeams } from "@/features/teams/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Global models view: pick a team, then manage its model deployments. */
export function ModelsPage() {
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const credentialNames = useCredentialNames();
  const [teamId, setTeamId] = useState("");
  const [offset, setOffset] = useState(0);
  const models = useQuery({
    queryKey: ["team-models", teamId, "page", offset],
    queryFn: () => listModelsPage(teamId, offset),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  useEffect(() => setOffset(0), [teamId]);
  useEffect(() => {
    if (!models.isFetching && models.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [models.data, models.isFetching, offset]);

  const error = toError(teams.error ?? models.error);

  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<Model | null>(null);
  const [deleting, setDeleting] = useState<Model | null>(null);

  return (
    <>
      <PageHeader
        command="models list"
        title="Models"
        description="Model deployments backed by provider credentials. Select a team to manage its models."
        actions={
          <Button size="sm" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            Add model
          </Button>
        }
      />
      <div className="mb-3 flex items-center gap-3">
        <Label htmlFor="models-team">team</Label>
        <select
          id="models-team"
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
      <ModelsTable
        rows={models.data?.items}
        isLoading={teams.isLoading || (teamId.length > 0 && models.isLoading)}
        error={error}
        credentialNames={credentialNames}
        onEdit={setEditing}
        onDelete={setDeleting}
      />
      {models.data ? (
        <PaginationControls
          offset={models.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={models.data.items.length}
          hasNext={models.data.hasNext}
          isFetching={models.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}

      <CreateModelDialog open={createOpen} onOpenChange={setCreateOpen} />
      <EditModelDialog
        model={editing}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
      />
      <DeleteModelDialog
        model={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </>
  );
}
