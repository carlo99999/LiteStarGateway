import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { PaginationControls } from "@/components/common/PaginationControls";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { IssueKeyDialog } from "@/features/api-keys/IssueKeyDialog";
import { KeysTable, type KeyRow } from "@/features/api-keys/KeysTable";
import { RevokeKeyDialog } from "@/features/api-keys/RevokeKeyDialog";
import { RotateKeyDialog } from "@/features/api-keys/RotateKeyDialog";
import { listTeamKeysPage, type ApiKey } from "@/features/api-keys/api";
import { useAuth } from "@/features/auth/use-auth";
import { useServicePrincipalNames } from "@/features/service-principals/useServicePrincipalNames";
import { listAllTeams } from "@/features/teams/api";
import { TABLE_PAGE_SIZE, previousPageOffset } from "@/lib/api/pagination";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Global API-keys view without the previous teams × keys request fan-out. */
export function ApiKeysPage() {
  const { user } = useAuth();
  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    retry: false,
  });
  const [teamId, setTeamId] = useState("");
  const [offset, setOffset] = useState(0);
  const spNames = useServicePrincipalNames(teamId || undefined);
  const selectedTeam = teams.data?.find((team) => team.id === teamId);
  const keys = useQuery({
    queryKey: ["team-keys", teamId, "page", offset],
    queryFn: () => listTeamKeysPage(teamId, offset),
    enabled: teamId.length > 0,
  });

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  useEffect(() => setOffset(0), [teamId]);
  useEffect(() => {
    if (!keys.isFetching && keys.data?.items.length === 0 && offset > 0) {
      setOffset(previousPageOffset(offset));
    }
  }, [keys.data, keys.isFetching, offset]);

  const rows: KeyRow[] | undefined = keys.data?.items.map((key) => ({
    ...key,
    teamName: selectedTeam?.name,
  }));
  const error = (teams.error ?? keys.error ?? null) as Error | null;

  const [issueOpen, setIssueOpen] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [rotating, setRotating] = useState<ApiKey | null>(null);

  return (
    <>
      <PageHeader
        command="api-keys list"
        title="API Keys"
        description="Select a team to manage its active and revoked keys without loading every team at once."
        actions={
          <Button size="sm" onClick={() => setIssueOpen(true)}>
            <Plus className="h-4 w-4" />
            Issue key
          </Button>
        }
      />
      <div className="mb-3 flex items-center gap-3">
        <Label htmlFor="keys-team">team</Label>
        <select
          id="keys-team"
          className={SELECT_CLASS}
          value={teamId}
          onChange={(event) => setTeamId(event.target.value)}
          disabled={teams.isLoading || teams.isError}
        >
          <option value="" disabled>
            {teams.isLoading ? "loading…" : teams.isError ? "failed to load teams" : "select a team"}
          </option>
          {(teams.data ?? []).map((team) => (
            <option key={team.id} value={team.id}>
              {team.name}
            </option>
          ))}
        </select>
      </div>
      <KeysTable
        rows={rows}
        isLoading={teams.isLoading || (teamId.length > 0 && keys.isLoading)}
        error={error}
        showTeam
        currentUserId={user?.id}
        servicePrincipalNames={spNames}
        onRotate={setRotating}
        onRevoke={setRevoking}
        emptyDescription={teamId ? "Issue a key with the button above." : "Select a team."}
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

      <IssueKeyDialog open={issueOpen} onOpenChange={setIssueOpen} />
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
    </>
  );
}
