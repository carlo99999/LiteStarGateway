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
import { updateCredential, type Credential } from "@/features/credentials/api";
import { PROVIDER_FIELDS, PROVIDER_LABELS } from "@/features/credentials/providerFields";

interface EditCredentialDialogProps {
  /** The credential to edit; null closes the dialog. */
  credential: Credential | null;
  onOpenChange: (open: boolean) => void;
}

/** Rename a credential and/or rotate its secrets. The provider is immutable
 * (recreate to change it). Secrets are never revealed, so the value fields
 * start blank: fill them ONLY to replace — leaving them blank keeps the current
 * ones. Any value entered replaces the whole set (required fields become
 * required only once you start rotating). */
export function EditCredentialDialog({ credential, onOpenChange }: EditCredentialDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});

  useEffect(() => {
    if (credential) {
      setName(credential.name);
      setValues({});
    }
  }, [credential]);

  const fields = credential ? PROVIDER_FIELDS[credential.provider] : [];
  const rotating = fields.some((f) => (values[f.key] ?? "").trim().length > 0);
  // If rotating, all required fields must be present (it's a full replacement).
  const missingRequired =
    rotating && fields.some((f) => f.required && !(values[f.key] ?? "").trim());

  const mutation = useMutation({
    mutationFn: () => {
      const changes: { name?: string; values?: Record<string, string> } = {};
      if (name.trim() && name.trim() !== credential!.name) changes.name = name.trim();
      if (rotating) {
        const payload: Record<string, string> = {};
        for (const field of fields) {
          const value = (values[field.key] ?? "").trim();
          if (value) payload[field.key] = value;
        }
        changes.values = payload;
      }
      return updateCredential(credential!.id, changes);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["credentials"] });
      onOpenChange(false);
    },
  });

  const nameChanged = credential ? name.trim() !== credential.name && name.trim().length > 0 : false;
  const canSubmit = !mutation.isPending && !missingRequired && (nameChanged || rotating);

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  return (
    <Dialog
      open={credential !== null}
      onOpenChange={(next) => {
        if (!next) mutation.reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit credential</DialogTitle>
          <DialogDescription>
            {credential ? (
              <>
                {PROVIDER_LABELS[credential.provider]} · provider is immutable. Leave the secret
                fields blank to keep the current values; fill them to rotate.
              </>
            ) : null}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="edit-cred-name">name</Label>
            <Input
              id="edit-cred-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
            />
          </div>
          {fields.map((field) => (
            <div key={field.key} className="grid gap-2">
              <Label htmlFor={`edit-cred-${field.key}`}>
                {field.label}
                <span className="ml-1 text-muted-foreground">
                  {field.required ? "(blank = keep)" : "(optional)"}
                </span>
              </Label>
              <Input
                id={`edit-cred-${field.key}`}
                type={field.secret ? "password" : "text"}
                value={values[field.key] ?? ""}
                onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
                placeholder={field.secret ? "•••••• (unchanged)" : field.placeholder}
                autoComplete="off"
              />
            </div>
          ))}
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
              {mutation.isPending ? "saving…" : "save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
