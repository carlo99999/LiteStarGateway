import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { IssueKeyDialog } from "@/features/api-keys/IssueKeyDialog";
import { KeysTable } from "@/features/api-keys/KeysTable";
import { RevokeKeyDialog } from "@/features/api-keys/RevokeKeyDialog";
import { RotateKeyDialog } from "@/features/api-keys/RotateKeyDialog";
import { listTeamKeysPage, type ApiKey } from "@/features/api-keys/api";
import { useAuth } from "@/features/auth/use-auth";
import { useServicePrincipalNames } from "@/features/service-principals/useServicePrincipalNames";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";
import { toError } from "@/lib/toError";

interface TeamKeysProps {
  teamId: string;
}

/** API keys for one team (team detail): list + issue + revoke. */
export function TeamKeys({ teamId }: TeamKeysProps) {
  const { user } = useAuth();
  const spNames = useServicePrincipalNames(teamId);
  const [offset, setOffset] = useState(0);
  const keys = useQuery({
    queryKey: ["team-keys", teamId, "page", offset],
    queryFn: () => listTeamKeysPage(teamId, offset),
  });
  const [issueOpen, setIssueOpen] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [rotating, setRotating] = useState<ApiKey | null>(null);

  useEffect(() => setOffset(0), [teamId]);
  useEffect(() => {
    if (!keys.isFetching && keys.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [keys.data, keys.isFetching, offset]);

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
          // api keys · all statuses
        </p>
        <Button size="sm" onClick={() => setIssueOpen(true)}>
          <Plus className="h-4 w-4" />
          Issue key
        </Button>
      </div>
      <KeysTable
        rows={keys.data?.items}
        isLoading={keys.isLoading}
        error={toError(keys.error)}
        showTeam={false}
        currentUserId={user?.id}
        servicePrincipalNames={spNames}
        onRotate={setRotating}
        onRevoke={setRevoking}
        emptyDescription="Issue a key to call the gateway on this team's behalf."
      />
      {keys.data ? (
        <PaginationControls
          offset={keys.data.offset}
          pageSize={TABLE_PAGE_SIZE}
          itemCount={keys.data.items.length}
          hasNext={keys.data.hasNext}
          isFetching={keys.isFetching}
          onOffsetChange={setOffset}
        />
      ) : null}

      <IssueKeyDialog teamId={teamId} open={issueOpen} onOpenChange={setIssueOpen} />
      <RotateKeyDialog
        apiKey={rotating}
        onOpenChange={(open) => {
          if (!open) setRotating(null);
        }}
      />
      <RevokeKeyDialog
        apiKey={revoking}
        onOpenChange={(open) => {
          if (!open) setRevoking(null);
        }}
      />
    </div>
  );
}
