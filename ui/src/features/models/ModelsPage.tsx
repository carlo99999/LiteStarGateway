import { useQuery } from "@tanstack/react-query";
import { Globe, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/features/auth/use-auth";
import { useCredentialNames } from "@/features/credentials/useCredentialNames";
import { CallableModelsTable } from "@/features/models/CallableModelsTable";
import { CreateModelDialog } from "@/features/models/CreateModelDialog";
import { DeleteModelDialog } from "@/features/models/DeleteModelDialog";
import { EditModelDialog } from "@/features/models/EditModelDialog";
import { ExtendModelDialog } from "@/features/models/ExtendModelDialog";
import { ModelsTable } from "@/features/models/ModelsTable";
import { listAllGlobalModels, listCallableModels, type Model } from "@/features/models/api";
import { canManageModels } from "@/features/teams/access";
import { useAccessibleTeams } from "@/features/teams/useAccessibleTeams";
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
  const { user } = useAuth();
  const teams = useAccessibleTeams();
  const isPlatformAdmin = Boolean(user?.is_admin);
  const globalModels = useQuery({
    queryKey: ["global-models"],
    queryFn: ({ signal }) => listAllGlobalModels(signal),
    enabled: isPlatformAdmin,
    retry: false,
  });

  const [teamId, setTeamId] = useState("");
  const credentialNames = useCredentialNames(
    isPlatformAdmin,
    isPlatformAdmin ? undefined : teamId,
  );
  const models = useQuery({
    queryKey: ["team-models", teamId, "callable"],
    queryFn: ({ signal }) => listCallableModels(teamId, signal),
    enabled: teamId.length > 0,
  });
  const teamNames = useMemo(
    () => new Map((teams.data ?? []).map((t) => [t.id, t.name])),
    [teams.data],
  );
  const selectedTeam = teams.data?.find((team) => team.id === teamId);
  const canManageSelectedTeam = selectedTeam ? canManageModels(selectedTeam.role) : false;

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);

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
        actions={isPlatformAdmin ? (
          <Button size="sm" onClick={() => setCreateGlobalOpen(true)}>
            <Globe className="h-4 w-4" />
            Add global model
          </Button>
        ) : undefined}
      />

      {/* Global (platform) models. */}
      {isPlatformAdmin ? (
        <>
          <p className={`mb-2 ${SECTION_LABEL}`}>// global models · every team</p>
          <ModelsTable
            rows={globalModels.data}
            isLoading={globalModels.isLoading}
            error={toError(globalModels.error)}
            credentialNames={credentialNames}
            onEdit={setEditing}
            onDelete={setDeleting}
          />
        </>
      ) : null}

      {/* Team-owned models. */}
      <div
        className={`${isPlatformAdmin ? "mt-8" : ""} mb-2 flex items-center justify-between gap-3`}
      >
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
        {canManageSelectedTeam ? (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setCreateTeamOpen(true)}
            disabled={!teamId}
          >
            <Plus className="h-4 w-4" />
            Add team model
          </Button>
        ) : null}
      </div>
      <CallableModelsTable
        rows={models.data}
        isLoading={teams.isLoading || (teamId.length > 0 && models.isLoading)}
        error={toError(teams.error ?? models.error)}
        credentialNames={credentialNames}
        teamNames={teamNames}
        onEdit={setEditing}
        onDelete={setDeleting}
        onExtend={setExtending}
        canManage={canManageSelectedTeam}
        canExtend={isPlatformAdmin}
      />

      {canManageSelectedTeam ? (
        <CreateModelDialog
          open={createTeamOpen}
          teamId={teamId}
          onOpenChange={setCreateTeamOpen}
        />
      ) : null}
      {isPlatformAdmin ? (
        <CreateModelDialog global open={createGlobalOpen} onOpenChange={setCreateGlobalOpen} />
      ) : null}
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
