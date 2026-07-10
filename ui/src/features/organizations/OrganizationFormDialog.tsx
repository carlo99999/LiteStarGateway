import { useMutation, useQueryClient } from "@tanstack/react-query";
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
  createOrganization,
  updateOrganization,
  type Organization,
  type OrganizationInput,
} from "@/features/organizations/api";

interface OrganizationFormDialogProps {
  mode: "create" | "edit";
  /** The organization being edited, when mode is "edit". */
  organization?: Organization;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Parse the comma-separated tags field into a trimmed, de-duplicated list. */
function parseTags(text: string): string[] {
  const seen = new Set<string>();
  const tags: string[] = [];
  for (const raw of text.split(",")) {
    const tag = raw.trim();
    if (tag && !seen.has(tag)) {
      seen.add(tag);
      tags.push(tag);
    }
  }
  return tags;
}

/** Create a new organization or edit an existing one (name, description, tags). */
export function OrganizationFormDialog({
  mode,
  organization,
  open,
  onOpenChange,
}: OrganizationFormDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tagsText, setTagsText] = useState("");

  // Seed the fields each time the dialog opens so a previous edit never leaks in.
  useEffect(() => {
    if (!open) return;
    if (mode === "edit" && organization) {
      setName(organization.name);
      setDescription(organization.description ?? "");
      setTagsText(organization.tags.join(", "));
    } else {
      setName("");
      setDescription("");
      setTagsText("");
    }
  }, [open, mode, organization]);

  const mutation = useMutation({
    mutationFn: (input: OrganizationInput) =>
      mode === "edit" && organization
        ? updateOrganization(organization.id, input)
        : createOrganization(input),
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: ["organizations"] });
      // Refresh the detail view too, when editing from there.
      if (mode === "edit") {
        await queryClient.invalidateQueries({ queryKey: ["organizations", saved.id] });
      }
      onOpenChange(false);
    },
  });

  const canSubmit = name.trim().length > 0 && !mutation.isPending;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    mutation.mutate({
      name: name.trim(),
      description: description.trim() || null,
      tags: parseTags(tagsText),
    });
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{mode === "edit" ? "Edit organization" : "New organization"}</DialogTitle>
          <DialogDescription>
            {mode === "edit"
              ? "Update the name, description, and tags. Members and teams are unaffected."
              : "Create a platform-scoped tenant. You can add teams to it afterwards."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="org-name">name</Label>
            <Input
              id="org-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme Corp"
              autoFocus
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="org-description">description</Label>
            <Input
              id="org-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="optional"
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="org-tags">tags</Label>
            <Input
              id="org-tags"
              value={tagsText}
              onChange={(e) => setTagsText(e.target.value)}
              placeholder="comma, separated, labels"
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
              {mutation.isPending ? "saving…" : mode === "edit" ? "save" : "create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
