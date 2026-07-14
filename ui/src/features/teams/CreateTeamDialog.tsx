import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { listAllOrganizations } from "@/features/organizations/api";
import { createTeam, parseRpm, parseTags } from "@/features/teams/api";

interface CreateTeamDialogProps {
  /** Fixed organization (from the org detail page). Omit to show an org picker
   * (from the global Teams page). */
  organizationId?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Create a team. The admin email must be an existing user, who becomes the
 * team's first admin. When no organization is fixed, the user picks one. */
export function CreateTeamDialog({ organizationId, open, onOpenChange }: CreateTeamDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [orgId, setOrgId] = useState("");
  const [description, setDescription] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [rpmText, setRpmText] = useState("");

  // Only need the org list when no organization is fixed.
  const orgs = useQuery({
    queryKey: ["organizations", "all"],
    queryFn: ({ signal }) => listAllOrganizations(signal),
    enabled: open && !organizationId,
    retry: false,
  });

  useEffect(() => {
    if (open) {
      setName("");
      setAdminEmail("");
      setOrgId("");
      setDescription("");
      setTagsText("");
      setRpmText("");
    }
  }, [open]);

  const effectiveOrgId = organizationId ?? orgId;

  const mutation = useMutation({
    mutationFn: () =>
      createTeam(effectiveOrgId, adminEmail.trim(), {
        name: name.trim(),
        description: description.trim() || null,
        tags: parseTags(tagsText),
        rate_limit_rpm: parseRpm(rpmText),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["organizations", effectiveOrgId] });
      await queryClient.invalidateQueries({ queryKey: ["teams"] });
      onOpenChange(false);
    },
  });

  const canSubmit =
    effectiveOrgId.length > 0 &&
    name.trim().length > 0 &&
    adminEmail.trim().length > 0 &&
    !mutation.isPending;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New team</DialogTitle>
          <DialogDescription>
            Create a team{organizationId ? " in this organization" : ""}. The admin email must be
            an existing user — they become the team&apos;s first admin.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          {!organizationId ? (
            <div className="grid gap-2">
              <Label htmlFor="team-org">organization</Label>
              <select
                id="team-org"
                className={SELECT_CLASS}
                value={orgId}
                onChange={(e) => setOrgId(e.target.value)}
              >
                <option value="" disabled>
                  {orgs.isLoading
                    ? "loading…"
                    : orgs.isError
                      ? "failed to load organizations"
                      : "select an organization"}
                </option>
                {(orgs.data ?? []).map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
              {orgs.isError ? (
                <p className="font-mono text-xs text-destructive">{orgs.error.message}</p>
              ) : null}
            </div>
          ) : null}
          <div className="grid gap-2">
            <Label htmlFor="team-name">name</Label>
            <Input
              id="team-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Platform"
              autoFocus
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="team-admin">admin email</Label>
            <Input
              id="team-admin"
              type="email"
              value={adminEmail}
              onChange={(e) => setAdminEmail(e.target.value)}
              placeholder="lead@example.com"
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="team-description">description</Label>
            <Input
              id="team-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="optional"
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="team-tags">tags</Label>
            <Input
              id="team-tags"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
              placeholder="comma, separated, labels"
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="team-rpm">rate limit (req/min)</Label>
            <Input
              id="team-rpm"
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
              {mutation.isPending ? "creating…" : "create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
