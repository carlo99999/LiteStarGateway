import { useQueries, useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { IssueKeyDialog } from "@/features/api-keys/IssueKeyDialog";
import { KeyStatusToggle, type KeyStatus } from "@/features/api-keys/KeyStatusToggle";
import { KeysTable, type KeyRow } from "@/features/api-keys/KeysTable";
import { RevokeKeyDialog } from "@/features/api-keys/RevokeKeyDialog";
import { RotateKeyDialog } from "@/features/api-keys/RotateKeyDialog";
import { listTeamKeys, type ApiKey } from "@/features/api-keys/api";
import { useAuth } from "@/features/auth/use-auth";
import { listTeams } from "@/features/teams/api";

/** Global API-keys view: every team's keys in one table. Issue picks the team
 * in the dialog; revoke uses each row's own team. */
export function ApiKeysPage() {
  const { user } = useAuth();
  const teams = useQuery({ queryKey: ["teams"], queryFn: listTeams });

  // One query per team, keyed the same as the team page's list, so issuing or
  // revoking (which invalidates ["team-keys", teamId]) refreshes this view too.
  const keyQueries = useQueries({
    queries: (teams.data ?? []).map((t) => ({
      queryKey: ["team-keys", t.id],
      queryFn: () => listTeamKeys(t.id),
    })),
  });

  const rows: KeyRow[] = (teams.data ?? []).flatMap((t, i) =>
    (keyQueries[i]?.data ?? []).map((k) => ({ ...k, teamName: t.name })),
  );
  const isLoading = teams.isLoading || keyQueries.some((q) => q.isLoading);
  const error = (teams.error ?? keyQueries.find((q) => q.error)?.error ?? null) as Error | null;

  const [issueOpen, setIssueOpen] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [rotating, setRotating] = useState<ApiKey | null>(null);
  const [status, setStatus] = useState<KeyStatus>("active");

  const active = rows.filter((k) => k.is_active);
  const revoked = rows.filter((k) => !k.is_active);
  const shown = isLoading ? undefined : status === "active" ? active : revoked;

  return (
    <>
      <PageHeader
        command="api-keys list"
        title="API Keys"
        description="Every team's keys. Issue a key (choosing its team) or revoke an existing one."
        actions={
          <Button size="sm" onClick={() => setIssueOpen(true)}>
            <Plus className="h-4 w-4" />
            Issue key
          </Button>
        }
      />
      <div className="mb-3">
        <KeyStatusToggle
          value={status}
          onChange={setStatus}
          activeCount={active.length}
          revokedCount={revoked.length}
        />
      </div>
      <KeysTable
        rows={shown}
        isLoading={isLoading}
        error={error}
        showTeam
        currentUserId={user?.id}
        onRotate={setRotating}
        onRevoke={setRevoking}
        emptyDescription={
          status === "active" ? "Issue a key with the button above." : "No revoked keys."
        }
      />

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
