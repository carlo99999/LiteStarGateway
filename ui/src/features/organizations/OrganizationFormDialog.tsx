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
} from "@/features/organizations/api";

interface OrganizationFormDialogProps {
  mode: "create" | "edit";
  /** The organization being renamed, when mode is "edit". */
  organization?: Organization;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Create a new organization or rename an existing one. */
export function OrganizationFormDialog({
  mode,
  organization,
  open,
  onOpenChange,
}: OrganizationFormDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");

  // Seed the field each time the dialog opens (blank for create, current name
  // for rename) so a previous edit never leaks into the next open.
  useEffect(() => {
    if (open) setName(mode === "edit" ? (organization?.name ?? "") : "");
  }, [open, mode, organization]);

  const mutation = useMutation({
    mutationFn: (value: string) =>
      mode === "edit" && organization
        ? updateOrganization(organization.id, value)
        : createOrganization(value),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["organizations"] });
      onOpenChange(false);
    },
  });

  const trimmed = name.trim();
  const unchanged = mode === "edit" && trimmed === organization?.name;
  const canSubmit = trimmed.length > 0 && !unchanged && !mutation.isPending;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate(trimmed);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{mode === "edit" ? "Rename organization" : "New organization"}</DialogTitle>
          <DialogDescription>
            {mode === "edit"
              ? "Change the display name. The id and members are unaffected."
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
              {mutation.isPending
                ? "saving…"
                : mode === "edit"
                  ? "save"
                  : "create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
