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
import { updateModel, type Model } from "@/features/models/api";

interface EditModelDialogProps {
  /** The model to edit; null closes the dialog. */
  model: Model | null;
  onOpenChange: (open: boolean) => void;
}

/** Parse an optional positive number field: blank → null; otherwise the number,
 * or null when it isn't a valid positive value. */
function parsePositive(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Parse an optional cost field where an explicit 0 is meaningful: blank → null,
 * otherwise the number if it's a finite value ≥ 0, else null. */
function parseNonNegative(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

function costText(perToken: number | null): string {
  return perToken === null ? "" : String(perToken);
}

/** Edit a model's mutable fields (upstream id, costs, ceiling, api version).
 * `name`, `provider` and `credential` are immutable — recreate to change them,
 * so they're shown read-only for orientation. */
export function EditModelDialog({ model, onOpenChange }: EditModelDialogProps) {
  const queryClient = useQueryClient();
  const [providerModelId, setProviderModelId] = useState("");
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [apiVersion, setApiVersion] = useState("");
  const [inputCost, setInputCost] = useState("");
  const [outputCost, setOutputCost] = useState("");

  // Re-seed the form whenever a different model is opened.
  useEffect(() => {
    if (model) {
      setProviderModelId(model.provider_model_id);
      setMaxOutputTokens(model.max_output_tokens === null ? "" : String(model.max_output_tokens));
      setApiVersion(model.api_version ?? "");
      setInputCost(costText(model.input_cost_per_token));
      setOutputCost(costText(model.output_cost_per_token));
    }
  }, [model]);

  const mutation = useMutation({
    mutationFn: () =>
      updateModel(model!.team_id, model!.id, {
        providerModelId: providerModelId.trim(),
        maxOutputTokens: parsePositive(maxOutputTokens),
        apiVersion: apiVersion.trim() || null,
        inputCostPerToken: parseNonNegative(inputCost),
        outputCostPerToken: parseNonNegative(outputCost),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["team-models", model!.team_id] });
      onOpenChange(false);
    },
  });

  const canSubmit = !mutation.isPending && providerModelId.trim().length > 0;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  return (
    <Dialog
      open={model !== null}
      onOpenChange={(next) => {
        if (!next) mutation.reset();
        onOpenChange(next);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Edit model</DialogTitle>
          <DialogDescription>
            {model ? (
              <>
                <span className="text-foreground">{model.name}</span> · {model.type} · immutable:
                provider &amp; credential (recreate to change those).
              </>
            ) : null}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="edit-model-provider-id">provider model id</Label>
            <Input
              id="edit-model-provider-id"
              value={providerModelId}
              onChange={(e) => setProviderModelId(e.target.value)}
              placeholder="the upstream id, e.g. gpt-4o-2024-08-06"
              autoComplete="off"
            />
          </div>
          {model?.provider === "azure_openai" ? (
            <div className="grid gap-2">
              <Label htmlFor="edit-model-api-version">
                api version<span className="ml-1 text-muted-foreground">(optional)</span>
              </Label>
              <Input
                id="edit-model-api-version"
                value={apiVersion}
                onChange={(e) => setApiVersion(e.target.value)}
                placeholder="overrides the credential's api_version"
                autoComplete="off"
              />
            </div>
          ) : null}
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-2">
              <Label htmlFor="edit-model-in-cost">
                input $/token<span className="ml-1 text-muted-foreground">(opt)</span>
              </Label>
              <Input
                id="edit-model-in-cost"
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
              <Label htmlFor="edit-model-out-cost">
                output $/token<span className="ml-1 text-muted-foreground">(opt)</span>
              </Label>
              <Input
                id="edit-model-out-cost"
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
            <Label htmlFor="edit-model-max-tokens">
              max output tokens<span className="ml-1 text-muted-foreground">(optional)</span>
            </Label>
            <Input
              id="edit-model-max-tokens"
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
              {mutation.isPending ? "saving…" : "save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
