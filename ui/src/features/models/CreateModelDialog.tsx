import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { listAllCredentials } from "@/features/credentials/api";
import { PROVIDER_LABELS, PROVIDERS } from "@/features/credentials/providerFields";
import { createModel, type ModelType, type Provider } from "@/features/models/api";
import { listAllTeams } from "@/features/teams/api";

interface CreateModelDialogProps {
  /** Fixed team (team detail page). Omit to show a team picker (global page). */
  teamId?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

const TYPES: ModelType[] = ["chat", "image", "embeddings"];

/** Parse an optional positive number field: blank → null; otherwise the number,
 * or null when it isn't a valid positive value. */
function parsePositive(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Parse an optional cost field where an explicit 0 is meaningful ("this model
 * is free"): blank → null (use the provider default), otherwise the number if
 * it's a finite value ≥ 0, else null. */
function parseNonNegative(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** Create a model deployment. The credential list is filtered to the chosen
 * provider, since the backend rejects a provider/credential mismatch. */
export function CreateModelDialog({ teamId, open, onOpenChange }: CreateModelDialogProps) {
  const queryClient = useQueryClient();
  const [teamPick, setTeamPick] = useState("");
  const [name, setName] = useState("");
  const [provider, setProvider] = useState<Provider>("openai");
  const [credentialId, setCredentialId] = useState("");
  const [type, setType] = useState<ModelType>("chat");
  const [providerModelId, setProviderModelId] = useState("");
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [apiVersion, setApiVersion] = useState("");
  const [inputCost, setInputCost] = useState("");
  const [outputCost, setOutputCost] = useState("");

  useEffect(() => {
    if (open) {
      setTeamPick("");
      setName("");
      setProvider("openai");
      setCredentialId("");
      setType("chat");
      setProviderModelId("");
      setMaxOutputTokens("");
      setApiVersion("");
      setInputCost("");
      setOutputCost("");
    }
  }, [open]);

  // A credential belongs to one provider; reset the pick when the provider changes.
  useEffect(() => setCredentialId(""), [provider]);

  const effectiveTeamId = teamId ?? teamPick;

  const teams = useQuery({
    queryKey: ["teams", "all"],
    queryFn: ({ signal }) => listAllTeams(signal),
    enabled: open && !teamId,
  });
  const credentials = useQuery({
    queryKey: ["credentials", "all"],
    queryFn: ({ signal }) => listAllCredentials(signal),
    enabled: open,
  });
  const providerCredentials = useMemo(
    () => (credentials.data ?? []).filter((c) => c.provider === provider),
    [credentials.data, provider],
  );

  const mutation = useMutation({
    mutationFn: () =>
      createModel(effectiveTeamId, {
        name: name.trim(),
        provider,
        credentialId,
        type,
        providerModelId: providerModelId.trim(),
        maxOutputTokens: parsePositive(maxOutputTokens),
        apiVersion: apiVersion.trim() || null,
        inputCostPerToken: parseNonNegative(inputCost),
        outputCostPerToken: parseNonNegative(outputCost),
        enabled: true,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["team-models", effectiveTeamId] });
      onOpenChange(false);
    },
  });

  const canSubmit =
    !mutation.isPending &&
    effectiveTeamId.length > 0 &&
    name.trim().length > 0 &&
    credentialId.length > 0 &&
    providerModelId.trim().length > 0;

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
          <DialogTitle>Add model</DialogTitle>
          <DialogDescription>
            A model deployment backed by one of this team&apos;s provider credentials. The
            provider must match the chosen credential.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          {!teamId ? (
            <div className="grid gap-2">
              <Label htmlFor="model-team">team</Label>
              <select
                id="model-team"
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
            <Label htmlFor="model-name">name</Label>
            <Input
              id="model-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. gpt-4o"
              autoComplete="off"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="model-provider">provider</Label>
            <select
              id="model-provider"
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
          <div className="grid gap-2">
            <Label htmlFor="model-credential">credential</Label>
            <select
              id="model-credential"
              className={SELECT_CLASS}
              value={credentialId}
              onChange={(e) => setCredentialId(e.target.value)}
            >
              <option value="" disabled>
                {credentials.isLoading
                  ? "loading…"
                  : providerCredentials.length === 0
                    ? `no ${PROVIDER_LABELS[provider]} credentials — add one first`
                    : "select a credential"}
              </option>
              {providerCredentials.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="model-type">type</Label>
            <select
              id="model-type"
              className={SELECT_CLASS}
              value={type}
              onChange={(e) => setType(e.target.value as ModelType)}
            >
              {TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="model-provider-id">provider model id</Label>
            <Input
              id="model-provider-id"
              value={providerModelId}
              onChange={(e) => setProviderModelId(e.target.value)}
              placeholder="the upstream id, e.g. gpt-4o-2024-08-06"
              autoComplete="off"
            />
          </div>
          {provider === "azure_openai" ? (
            <div className="grid gap-2">
              <Label htmlFor="model-api-version">
                api version<span className="ml-1 text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="model-api-version"
                value={apiVersion}
                onChange={(e) => setApiVersion(e.target.value)}
                placeholder="overrides the credential's api_version"
                autoComplete="off"
              />
            </div>
          ) : null}
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label htmlFor="model-in-cost">
                input $/token<span className="ml-1 text-muted-foreground">(opt)</span>
              </Label>
              <Input
                id="model-in-cost"
                type="number"
                step="any"
                min="0"
                value={inputCost}
                onChange={(e) => setInputCost(e.target.value)}
                placeholder="e.g. 0.0000025"
                autoComplete="off"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="model-out-cost">
                output $/token<span className="ml-1 text-muted-foreground">(opt)</span>
              </Label>
              <Input
                id="model-out-cost"
                type="number"
                step="any"
                min="0"
                value={outputCost}
                onChange={(e) => setOutputCost(e.target.value)}
                placeholder="e.g. 0.00001"
                autoComplete="off"
              />
            </div>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="model-max-tokens">
              max output tokens<span className="ml-1 text-muted-foreground">(optional)</span>
            </Label>
            <Input
              id="model-max-tokens"
              type="number"
              min="1"
              value={maxOutputTokens}
              onChange={(e) => setMaxOutputTokens(e.target.value)}
              placeholder="no ceiling"
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
              {mutation.isPending ? "adding…" : "add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
