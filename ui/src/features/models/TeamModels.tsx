import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { useCredentialNames } from "@/features/credentials/useCredentialNames";
import { CallableModelsTable } from "@/features/models/CallableModelsTable";
import { CreateModelDialog } from "@/features/models/CreateModelDialog";
import { DeleteModelDialog } from "@/features/models/DeleteModelDialog";
import { EditModelDialog } from "@/features/models/EditModelDialog";
import { ExtendModelDialog } from "@/features/models/ExtendModelDialog";
import { listCallableModels, type Model } from "@/features/models/api";
import { listAllTeams } from "@/features/teams/api";
import { toError } from "@/lib/toError";

interface TeamModelsProps {
  teamId: string;
}

/** Models for one team (team detail): everything it can call — its own models,
 * models extended to it, and global models — plus add/edit/enable/extend/delete
 * on its own. */
export function TeamModels({ teamId }: TeamModelsProps) {
  const credentialNames = useCredentialNames();
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
  });
  const teamNames = useMemo(
    () => new Map((teams.data ?? []).map((t) => [t.id, t.name])),
    [teams.data],
  );
  const models = useQuery({
    queryKey: ["team-models", teamId, "callable"],
    queryFn: ({ signal }) => listCallableModels(teamId, signal),
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [editing, setEditing] = useState<Model | null>(null);
  const [deleting, setDeleting] = useState<Model | null>(null);
  const [extending, setExtending] = useState<Model | null>(null);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // models · own, extended &amp; global
        </p>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          Add model
        </Button>
      </div>
      <CallableModelsTable
        rows={models.data}
        isLoading={models.isLoading}
        error={toError(models.error)}
        credentialNames={credentialNames}
        teamNames={teamNames}
        onEdit={setEditing}
        onDelete={setDeleting}
        onExtend={setExtending}
        canManage
        canExtend
      />

      <CreateModelDialog teamId={teamId} open={createOpen} onOpenChange={setCreateOpen} />
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
    </div>
  );
}
