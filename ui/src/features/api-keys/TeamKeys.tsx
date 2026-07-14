import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { IssueKeyDialog } from "@/features/api-keys/IssueKeyDialog";
import { KeyStatusToggle, type KeyStatus } from "@/features/api-keys/KeyStatusToggle";
import { KeysTable } from "@/features/api-keys/KeysTable";
import { RevokeKeyDialog } from "@/features/api-keys/RevokeKeyDialog";
import { RotateKeyDialog } from "@/features/api-keys/RotateKeyDialog";
import { listTeamKeys, type ApiKey } from "@/features/api-keys/api";
import { useAuth } from "@/features/auth/use-auth";

interface TeamKeysProps {
  teamId: string;
}

/** API keys for one team (team detail): list + issue + revoke. */
export function TeamKeys({ teamId }: TeamKeysProps) {
  const { user } = useAuth();
  const keys = useQuery({ queryKey: ["team-keys", teamId], queryFn: () => listTeamKeys(teamId) });
  const [issueOpen, setIssueOpen] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [rotating, setRotating] = useState<ApiKey | null>(null);
  const [status, setStatus] = useState<KeyStatus>("active");

  const all = keys.data ?? [];
  const active = all.filter((k) => k.is_active);
  const revoked = all.filter((k) => !k.is_active);
  const shown = keys.data ? (status === "active" ? active : revoked) : undefined;

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
            // api keys
          </p>
          <KeyStatusToggle
            value={status}
            onChange={setStatus}
            activeCount={active.length}
            revokedCount={revoked.length}
          />
        </div>
        <Button size="sm" onClick={() => setIssueOpen(true)}>
          <Plus className="h-4 w-4" />
          Issue key
        </Button>
      </div>
      <KeysTable
        rows={shown}
        isLoading={keys.isLoading}
        error={keys.error as Error | null}
        showTeam={false}
        currentUserId={user?.id}
        onRotate={setRotating}
        onRevoke={setRevoking}
        emptyDescription={
          status === "active"
            ? "Issue a key to call the gateway on this team's behalf."
            : "No revoked keys."
        }
      />

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
