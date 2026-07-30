import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { listAllTeamKeys, type ApiKey } from "@/features/api-keys/api";
import {
  clearKeyToolPolicy,
  getKeyToolPolicy,
  setKeyToolPolicy,
} from "@/features/tools/api";
import {
  DESTRUCTIVE_TOGGLE_HELP,
  DESTRUCTIVE_TOGGLE_LABEL,
  describePolicy,
} from "@/features/tools/inventory";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Which tools one API key may invoke.
 *
 * Under `tools:read`/`tools:manage` on both sides, not `keys:issue` — Round 15's
 * ISSUE-042 was per-key spend caps whose write and read landed in different
 * permission domains, so an issuer could save an object and then be refused the
 * read of it.
 *
 * Absent policy means unrestricted, the polarity a missing spend cap already has:
 * a key with no row may call every tool except those declared destructive. So the
 * panel opens on that state and describes it as the default rather than as
 * something missing.
 */
export function KeyToolPolicyPanel({ teamId }: { teamId: string }) {
  const queryClient = useQueryClient();
  const [keyId, setKeyId] = useState("");
  const [allowed, setAllowed] = useState("");
  const [destructive, setDestructive] = useState(false);

  const keys = useQuery({
    queryKey: ["teams", teamId, "keys", "all"],
    queryFn: () => listAllTeamKeys(teamId),
    enabled: teamId.length > 0,
  });
  const policy = useQuery({
    queryKey: ["teams", teamId, "keys", keyId, "tool-policy"],
    queryFn: () => getKeyToolPolicy(teamId, keyId),
    enabled: keyId.length > 0,
  });

  useEffect(() => {
    setKeyId("");
  }, [teamId]);
  useEffect(() => {
    // Mirror the stored policy into the form whenever a different key is picked,
    // so the switch shows what is in force rather than what was last typed.
    if (!policy.data) return;
    setAllowed(policy.data.allowed_tools.join(", "));
    setDestructive(policy.data.destructive_enabled);
  }, [policy.data]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["teams", teamId, "keys", keyId, "tool-policy"] });

  const save = useMutation({
    mutationFn: () =>
      setKeyToolPolicy(teamId, keyId, {
        allowed_tools: allowed
          .split(",")
          .map((name) => name.trim())
          .filter(Boolean),
        destructive_enabled: destructive,
      }),
    onSuccess: invalidate,
  });
  const clear = useMutation({
    mutationFn: () => clearKeyToolPolicy(teamId, keyId),
    onSuccess: invalidate,
  });

  const busy = save.isPending || clear.isPending;
  const mutationError = toError(save.error ?? clear.error)?.message ?? null;
  const activeKeys = (keys.data ?? []).filter((key: ApiKey) => key.is_active);

  return (
    <section className="mt-10 border-t border-border pt-6">
      <p className="mb-3 font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // per-key tool policy
      </p>
      <p className="mb-4 max-w-3xl font-mono text-xs text-muted-foreground">
        A key with no policy may invoke every tool except those declared
        destructive. Naming tools here narrows it; leaving the list empty keeps
        every tool available and changes only the switch below.
      </p>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Label htmlFor="tool-policy-key">key</Label>
        <select
          id="tool-policy-key"
          className={SELECT_CLASS + " min-w-64"}
          value={keyId}
          onChange={(event) => setKeyId(event.target.value)}
          disabled={keys.isLoading || keys.isError}
        >
          <option value="" disabled>
            {keys.isLoading
              ? "loading…"
              : keys.isError
                ? "failed to load keys"
                : "select a key"}
          </option>
          {activeKeys.map((key: ApiKey) => (
            <option key={key.id} value={key.id}>
              {key.name} [{key.prefix}]
            </option>
          ))}
        </select>
      </div>

      {keys.isError ? (
        <p className="font-mono text-xs text-destructive">
          ! couldn&apos;t load the team&apos;s keys — {toError(keys.error)?.message}
        </p>
      ) : null}

      {keyId && policy.isError ? (
        <p className="font-mono text-xs text-destructive">
          ! couldn&apos;t load the policy — {toError(policy.error)?.message}
        </p>
      ) : null}

      {keyId && policy.data ? (
        <div className="flex flex-col gap-4">
          <p className="font-mono text-xs text-muted-foreground">
            in force: {describePolicy(policy.data)}
          </p>
          <div className="grid max-w-2xl gap-2">
            <Label htmlFor="tool-policy-allowed">allowed tools (comma separated)</Label>
            <Input
              id="tool-policy-allowed"
              value={allowed}
              placeholder="leave empty for every tool"
              onChange={(event) => setAllowed(event.target.value)}
            />
          </div>
          <div className="flex max-w-2xl items-start gap-3">
            <input
              id="tool-policy-destructive"
              type="checkbox"
              className="mt-1"
              checked={destructive}
              onChange={(event) => setDestructive(event.target.checked)}
            />
            <div>
              <Label htmlFor="tool-policy-destructive">{DESTRUCTIVE_TOGGLE_LABEL}</Label>
              <p className="mt-1 max-w-xl font-mono text-xs text-muted-foreground">
                {DESTRUCTIVE_TOGGLE_HELP}
              </p>
            </div>
          </div>
          {mutationError ? (
            <p className="font-mono text-xs text-destructive">! {mutationError}</p>
          ) : null}
          <div className="flex gap-3">
            <Button onClick={() => save.mutate()} disabled={busy}>
              save policy
            </Button>
            <Button
              variant="outline"
              onClick={() => clear.mutate()}
              disabled={busy || !policy.data.restricted}
              // Removing the policy widens the key back to permissive, so the
              // label says that rather than "clear".
              title="makes this key permissive again"
            >
              remove restriction
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
