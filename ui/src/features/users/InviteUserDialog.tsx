import { useMutation, useQuery } from "@tanstack/react-query";
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
import { Label } from "@/components/ui/label";
import { buildInviteSignupLink } from "@/features/auth/inviteToken";
import { listTeams } from "@/features/teams/api";
import { inviteUser, type Invite, type TeamRole } from "@/features/users/api";

interface InviteUserDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

const ROLES: TeamRole[] = ["member", "admin", "model-manager", "key-issuer", "billing-viewer"];

/** Invite a new user: pick the team they join and their role there, then hand
 * them the one-time signup link/token to set their own password. The token is
 * shown once and never again. */
export function InviteUserDialog({ open, onOpenChange }: InviteUserDialogProps) {
  const [teamId, setTeamId] = useState("");
  const [role, setRole] = useState<TeamRole>("member");
  const [invite, setInvite] = useState<Invite | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (open) {
      setTeamId("");
      setRole("member");
      setInvite(null);
      setCopied(false);
    }
  }, [open]);

  const teams = useQuery({ queryKey: ["teams"], queryFn: listTeams, enabled: open });

  const mutation = useMutation({
    mutationFn: () => inviteUser(teamId, role),
    onSuccess: (created) => setInvite(created), // keep open to reveal the token once
  });

  const canSubmit = !mutation.isPending && teamId.length > 0;

  // Fragments stay client-side: the bearer never reaches HTTP access logs.
  const signupLink = invite ? buildInviteSignupLink(window.location.origin, invite.token) : "";

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  async function copy() {
    if (!signupLink) return;
    await navigator.clipboard.writeText(signupLink);
    setCopied(true);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {invite ? (
          <>
            <DialogHeader>
              <DialogTitle>Invite created</DialogTitle>
              <DialogDescription>
                Send this link to the invitee — they open it to set their password and activate
                the account. Copy it now; the token is shown only once.
              </DialogDescription>
            </DialogHeader>
            <div className="flex items-center gap-2 rounded-md border border-border bg-secondary p-2">
              <code className="flex-1 break-all font-mono text-xs text-foreground">
                {signupLink}
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
              <DialogTitle>Invite user</DialogTitle>
              <DialogDescription>
                The invitee joins the chosen team with the chosen role, then sets their own
                password via the invite token.
              </DialogDescription>
            </DialogHeader>
            <form onSubmit={handleSubmit} className="grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="invite-team">team</Label>
                <select
                  id="invite-team"
                  className={SELECT_CLASS}
                  value={teamId}
                  onChange={(e) => setTeamId(e.target.value)}
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
              <div className="grid gap-2">
                <Label htmlFor="invite-role">role</Label>
                <select
                  id="invite-role"
                  className={SELECT_CLASS}
                  value={role}
                  onChange={(e) => setRole(e.target.value as TeamRole)}
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
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
                  {mutation.isPending ? "inviting…" : "invite"}
                </Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
