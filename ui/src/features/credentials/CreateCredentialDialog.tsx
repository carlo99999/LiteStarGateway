import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type FormEvent } from "react";
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
import { createCredential, type Provider } from "@/features/credentials/api";
import { PROVIDER_FIELDS, PROVIDER_LABELS, PROVIDERS } from "@/features/credentials/providerFields";

interface CreateCredentialDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

/** Add a provider credential. The value fields shown depend on the provider;
 * secrets are encrypted at rest and can never be read back, so this dialog is
 * the only time they're entered. */
export function CreateCredentialDialog({ open, onOpenChange }: CreateCredentialDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [provider, setProvider] = useState<Provider>("openai");
  const [values, setValues] = useState<Record<string, string>>({});

  useEffect(() => {
    if (open) {
      setName("");
      setProvider("openai");
      setValues({});
    }
  }, [open]);

  // Reset entered values when the provider changes — the field set differs.
  useEffect(() => setValues({}), [provider]);

  const fields = PROVIDER_FIELDS[provider];

  const mutation = useMutation({
    mutationFn: () => {
      // Trim, and drop optional fields left blank so we never send empty values.
      const payload: Record<string, string> = {};
      for (const field of fields) {
        const value = (values[field.key] ?? "").trim();
        if (value) payload[field.key] = value;
      }
      return createCredential(name.trim(), provider, payload);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["credentials"] });
      onOpenChange(false);
    },
  });

  const missingRequired = useMemo(
    () => fields.some((field) => field.required && !(values[field.key] ?? "").trim()),
    [fields, values],
  );
  const canSubmit = !mutation.isPending && name.trim().length > 0 && !missingRequired;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) mutation.reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add credential</DialogTitle>
          <DialogDescription>
            Provider secrets are encrypted at rest and never shown again — enter them here once.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="cred-name">name</Label>
            <Input
              id="cred-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. prod-openai"
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="cred-provider">provider</Label>
            <select
              id="cred-provider"
              className={SELECT_CLASS}
              value={provider}
              onChange={(e) => setProvider(e.target.value as Provider)}
            >
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {PROVIDER_LABELS[p]}
                </option>
              ))}
            </select>
          </div>
          {fields.map((field) => (
            <div key={field.key} className="grid gap-2">
              <Label htmlFor={`cred-${field.key}`}>
                {field.label}
                {field.required ? null : (
                  <span className="ml-1 text-muted-foreground">(optional)</span>
                )}
              </Label>
              <Input
                id={`cred-${field.key}`}
                type={field.secret ? "password" : "text"}
                value={values[field.key] ?? ""}
                onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
                placeholder={field.placeholder}
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
              {mutation.isPending ? "adding…" : "add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
