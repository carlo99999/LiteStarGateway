import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { CreateServicePrincipalDialog } from "@/features/service-principals/CreateServicePrincipalDialog";
import { DeleteServicePrincipalDialog } from "@/features/service-principals/DeleteServicePrincipalDialog";
import { ServicePrincipalsTable } from "@/features/service-principals/ServicePrincipalsTable";
import {
  listServicePrincipalsPage,
  type ServicePrincipal,
} from "@/features/service-principals/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

interface TeamServicePrincipalsProps {
  teamId: string;
}

/** Service principals for one team (team detail): list + create + enable/disable
 * + delete. */
export function TeamServicePrincipals({ teamId }: TeamServicePrincipalsProps) {
  const [offset, setOffset] = useState(0);
  const sps = useQuery({
    queryKey: ["team-service-principals", teamId, "page", offset],
    queryFn: () => listServicePrincipalsPage(teamId, offset),
  });
  const [createOpen, setCreateOpen] = useState(false);
  const [deleting, setDeleting] = useState<ServicePrincipal | null>(null);

  useEffect(() => setOffset(0), [teamId]);
  useEffect(() => {
    if (!sps.isFetching && sps.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [sps.data, sps.isFetching, offset]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // service principals
        </p>
        <Button size="sm" onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          New service principal
        </Button>
      </div>
      <ServicePrincipalsTable
        rows={sps.data?.items}
        isLoading={sps.isLoading}
        error={toError(sps.error)}
        showTeam={false}
        onDelete={setDeleting}
        emptyDescription="Create a service principal to hold this team's management-scoped keys."
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

      <CreateServicePrincipalDialog teamId={teamId} open={createOpen} onOpenChange={setCreateOpen} />
      <DeleteServicePrincipalDialog
        sp={deleting}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
      />
    </div>
  );
}
