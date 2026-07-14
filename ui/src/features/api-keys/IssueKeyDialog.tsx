import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  issueServicePrincipalKey,
  issueTeamKey,
  listServicePrincipals,
  type IssuedKey,
  type KeyScope,
} from "@/features/api-keys/api";
import { listTeams, parseRpm } from "@/features/teams/api";

interface IssueKeyDialogProps {
  /** Fixed team (from the team detail page). Omit to show a team picker (from
   * the global /api-keys page). */
  teamId?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

type Subject = "personal" | "service_principal";

const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Issue an API key for a team — either personal (attributed to the current
 * account, inference-only) or for an existing service principal (which may hold
 * management/all scope). The plaintext is shown once, never again. */
export function IssueKeyDialog({ teamId, open, onOpenChange }: IssueKeyDialogProps) {
  const queryClient = useQueryClient();
  const [teamPick, setTeamPick] = useState("");
  const [subject, setSubject] = useState<Subject>("personal");
  const [name, setName] = useState("");
  const [rpmText, setRpmText] = useState("");
  const [spId, setSpId] = useState("");
  const [scope, setScope] = useState<KeyScope>("inference");
  const [issued, setIssued] = useState<IssuedKey | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (open) {
      setTeamPick("");
      setSubject("personal");
      setName("");
      setRpmText("");
      setSpId("");
      setScope("inference");
      setIssued(null);
      setCopied(false);
    }
  }, [open]);

  const effectiveTeamId = teamId ?? teamPick;

  // Team picker (only when no fixed team).
  const teams = useQuery({
    queryKey: ["teams"],
    queryFn: listTeams,
    enabled: open && !teamId,
  });

  // Service principals are team-scoped, so reset the pick when the team changes.
  useEffect(() => {
    setSpId("");
  }, [effectiveTeamId]);

  const sps = useQuery({
    queryKey: ["team-service-principals", effectiveTeamId],
    queryFn: () => listServicePrincipals(effectiveTeamId),
    enabled: open && subject === "service_principal" && effectiveTeamId.length > 0,
  });

  const mutation = useMutation({
    mutationFn: () => {
      const rpm = parseRpm(rpmText);
      const keyName = name.trim();
      return subject === "service_principal"
        ? issueServicePrincipalKey(effectiveTeamId, spId, keyName, scope, rpm)
        : issueTeamKey(effectiveTeamId, keyName, rpm);
    },
    onSuccess: async (key) => {
      await queryClient.invalidateQueries({ queryKey: ["team-keys", effectiveTeamId] });
      setIssued(key); // keep the dialog open to reveal the plaintext once
    },
  });

  const canSubmit =
    !mutation.isPending &&
    effectiveTeamId.length > 0 &&
    name.trim().length > 0 &&
    (subject === "personal" || spId.length > 0);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  async function copy() {
    if (!issued) return;
    await navigator.clipboard.writeText(issued.plaintext);
    setCopied(true);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {issued ? (
          <>
            <DialogHeader>
              <DialogTitle>API key created</DialogTitle>
              <DialogDescription>
                Copy it now — this is the only time the full key is shown.
              </DialogDescription>
            </DialogHeader>
            <div className="flex items-center gap-2 rounded-md border border-border bg-secondary p-2">
              <code className="flex-1 break-all font-mono text-xs text-foreground">
                {issued.plaintext}
              </code>
              <Button type="button" variant="outline" size="sm" onClick={copy}>
                {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                {copied ? "copied" : "copy"}
              </Button>
            </div>
            <DialogFooter>
              <Button type="button" onClick={() => onOpenChange(false)}>
                done
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Issue API key</DialogTitle>
              <DialogDescription>
                A personal key is attributed to your account (inference-only). A service-principal
                key belongs to a team identity and may hold management scope.
              </DialogDescription>
            </DialogHeader>
            <form onSubmit={handleSubmit} className="grid gap-4">
              {!teamId ? (
                <div className="grid gap-2">
                  <Label htmlFor="key-team">team</Label>
                  <select
                    id="key-team"
                    className={SELECT_CLASS}
                    value={teamPick}
                    onChange={(e) => setTeamPick(e.target.value)}
                  >
                    <option value="" disabled>
                      {teams.isLoading ? "loading…" : "select a team"}
                    </option>
                    {(teams.data ?? []).map((t) => (
                      <option key={t.id} value={t.id}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              <div className="grid gap-2">
                <Label htmlFor="key-subject">owner</Label>
                <select
                  id="key-subject"
                  className={SELECT_CLASS}
                  value={subject}
                  onChange={(e) => setSubject(e.target.value as Subject)}
                >
                  <option value="personal">personal (me)</option>
                  <option value="service_principal">service principal</option>
                </select>
              </div>

              {subject === "service_principal" ? (
                <>
                  <div className="grid gap-2">
                    <Label htmlFor="key-sp">service principal</Label>
                    <select
                      id="key-sp"
                      className={SELECT_CLASS}
                      value={spId}
                      onChange={(e) => setSpId(e.target.value)}
                      disabled={effectiveTeamId.length === 0}
                    >
                      <option value="" disabled>
                        {effectiveTeamId.length === 0
                          ? "select a team first"
                          : sps.isLoading
                            ? "loading…"
                            : "select a service principal"}
                      </option>
                      {(sps.data ?? []).map((sp) => (
                        <option key={sp.id} value={sp.id} disabled={!sp.enabled}>
                          {sp.name}
                          {sp.enabled ? "" : " (disabled)"}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="key-scope">scope</Label>
                    <select
                      id="key-scope"
                      className={SELECT_CLASS}
                      value={scope}
                      onChange={(e) => setScope(e.target.value as KeyScope)}
                    >
                      <option value="inference">inference</option>
                      <option value="management">management</option>
                      <option value="all">all</option>
                    </select>
                  </div>
                </>
              ) : (
                <p className="font-mono text-xs text-muted-foreground">
                  scope: <span className="text-foreground">inference</span> — personal keys
                  can&apos;t manage the team. For management/all, issue a key for a service
                  principal.
                </p>
              )}

              <div className="grid gap-2">
                <Label htmlFor="key-name">name</Label>
                <Input
                  id="key-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. ci-pipeline"
                  autoComplete="off"
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="key-rpm">rate limit (req/min)</Label>
                <Input
                  id="key-rpm"
                  type="number"
                  min="1"
                  value={rpmText}
                  onChange={(e) => setRpmText(e.target.value)}
                  placeholder="unlimited"
                  autoComplete="off"
                />
              </div>
              {mutation.isError ? (
                <p className="font-mono text-xs text-destructive">{mutation.error.message}</p>
              ) : null}
              <DialogFooter>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => onOpenChange(false)}
                  disabled={mutation.isPending}
                >
                  cancel
                </Button>
                <Button type="submit" disabled={!canSubmit}>
                  {mutation.isPending ? "issuing…" : "issue"}
                </Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
