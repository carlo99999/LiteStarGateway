import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { useCredentialNames } from "@/features/credentials/useCredentialNames";
import { CreateModelDialog } from "@/features/models/CreateModelDialog";
import { DeleteModelDialog } from "@/features/models/DeleteModelDialog";
import { ModelsTable } from "@/features/models/ModelsTable";
import { listModelsPage, type Model } from "@/features/models/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

interface TeamModelsProps {
  teamId: string;
}

/** Models for one team (team detail): list + add + enable/disable + delete. */
export function TeamModels({ teamId }: TeamModelsProps) {
  const credentialNames = useCredentialNames();
  const [offset, setOffset] = useState(0);
  const models = useQuery({
    queryKey: ["team-models", teamId, "page", offset],
    queryFn: () => listModelsPage(teamId, offset),
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<Model | null>(null);

  useEffect(() => setOffset(0), [teamId]);
  useEffect(() => {
    if (!models.isFetching && models.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [models.data, models.isFetching, offset]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // models
        </p>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          Add model
        </Button>
      </div>
      <ModelsTable
        rows={models.data?.items}
        isLoading={models.isLoading}
        error={toError(models.error)}
        credentialNames={credentialNames}
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

      <CreateModelDialog teamId={teamId} open={createOpen} onOpenChange={setCreateOpen} />
      <DeleteModelDialog
        model={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </div>
  );
}
