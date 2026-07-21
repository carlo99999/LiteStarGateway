import { useQuery } from "@tanstack/react-query";
import { Globe, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useCredentialNames } from "@/features/credentials/useCredentialNames";
import { CreateModelDialog } from "@/features/models/CreateModelDialog";
import { DeleteModelDialog } from "@/features/models/DeleteModelDialog";
import { EditModelDialog } from "@/features/models/EditModelDialog";
import { ExtendModelDialog } from "@/features/models/ExtendModelDialog";
import { ModelsTable } from "@/features/models/ModelsTable";
import { listAllGlobalModels, listModelsPage, type Model } from "@/features/models/api";
import { listAllTeams } from "@/features/teams/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

const SECTION_LABEL =
  "font-mono text-[10px] uppercase tracking-widest text-muted-foreground";

/** Models view: platform-wide global models, plus a per-team section for
 * team-owned models (which can be extended to other teams). */
export function ModelsPage() {
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const credentialNames = useCredentialNames();
  const globalModels = useQuery({
    queryKey: ["global-models"],
    queryFn: ({ signal }) => listAllGlobalModels(signal),
    retry: false,
  });

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

  const [createTeamOpen, setCreateTeamOpen] = useState(false);
  const [createGlobalOpen, setCreateGlobalOpen] = useState(false);
  const [editing, setEditing] = useState<Model | null>(null);
  const [deleting, setDeleting] = useState<Model | null>(null);
  const [extending, setExtending] = useState<Model | null>(null);

  return (
    <>
      <PageHeader
        command="models list"
        title="Models"
        description="Global models are callable by every team. Team models belong to one team and can be extended to others."
        actions={
          <Button size="sm" onClick={() => setCreateGlobalOpen(true)}>
            <Globe className="h-4 w-4" />
            Add global model
          </Button>
        }
      />

      {/* Global (platform) models. */}
      <p className={`mb-2 ${SECTION_LABEL}`}>// global models · every team</p>
      <ModelsTable
        rows={globalModels.data}
        isLoading={globalModels.isLoading}
        error={toError(globalModels.error)}
        credentialNames={credentialNames}
        onEdit={setEditing}
        onDelete={setDeleting}
      />

      {/* Team-owned models. */}
      <div className="mb-2 mt-8 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <p className={SECTION_LABEL}>// team models</p>
          <Label htmlFor="models-team" className="sr-only">
            team
          </Label>
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
        <Button size="sm" variant="outline" onClick={() => setCreateTeamOpen(true)} disabled={!teamId}>
          <Plus className="h-4 w-4" />
          Add team model
        </Button>
      </div>
      <ModelsTable
        rows={models.data?.items}
        isLoading={teams.isLoading || (teamId.length > 0 && models.isLoading)}
        error={toError(teams.error ?? models.error)}
        credentialNames={credentialNames}
        onEdit={setEditing}
        onDelete={setDeleting}
        onExtend={setExtending}
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

      <CreateModelDialog open={createTeamOpen} teamId={teamId} onOpenChange={setCreateTeamOpen} />
      <CreateModelDialog global open={createGlobalOpen} onOpenChange={setCreateGlobalOpen} />
      <EditModelDialog
        model={editing}
        onOpenChange={(open) => {
          if (!open) setEditing(null);
        }}
      />
      <ExtendModelDialog
        model={extending}
        onOpenChange={(open) => {
          if (!open) setExtending(null);
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
