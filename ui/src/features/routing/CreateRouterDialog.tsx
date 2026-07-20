import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
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
import { listAllModels } from "@/features/models/api";
import { createRouter, type CandidateRequest } from "@/features/routing/api";
import {
  QUALITY_TIERS,
  SHADOWABLE_STRATEGIES,
  STRATEGY_HELP,
  STRATEGY_IDS,
  STRATEGY_LABELS,
  type QualityTier,
  type StrategyId,
} from "@/features/routing/strategies";

interface CreateRouterDialogProps {
  teamId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const SELECT_CLASS =
  "flex h-9 w-full rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

interface CandidateRow {
  modelName: string;
  description: string;
  tier: QualityTier;
  vision: boolean;
  tools: boolean;
  jsonSchema: boolean;
  weight: string;
}

interface RouteRow {
  name: string;
  targetModel: string;
  utterances: string;
  threshold: string;
}

const EMPTY_CANDIDATE: CandidateRow = {
  modelName: "",
  description: "",
  tier: "MEDIUM",
  vision: false,
  tools: false,
  jsonSchema: false,
  weight: "",
};

const EMPTY_ROUTE: RouteRow = { name: "", targetModel: "", utterances: "", threshold: "" };

function parsePositive(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** A route threshold: blank → null (backend default 0.80); otherwise valid only
 * in (0, 1] per the embeddings contract, else null. */
function parseThreshold(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n > 0 && n <= 1 ? n : null;
}

/** True when a threshold field is acceptable to submit: blank (use default) or
 * a valid (0, 1] value. A non-blank out-of-range value is rejected in-form
 * instead of being silently dropped to the default. */
function thresholdValid(text: string): boolean {
  return text.trim() === "" || parseThreshold(text) !== null;
}

/** Create a smart router (virtual model). Candidates reference the team's
 * models by name; the strategy picks among them per request. Per-candidate
 * costs are omitted here — the gateway falls back to the model's own costs. */
export function CreateRouterDialog({ teamId, open, onOpenChange }: CreateRouterDialogProps) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [strategy, setStrategy] = useState<StrategyId>("complexity");
  const [candidates, setCandidates] = useState<CandidateRow[]>([EMPTY_CANDIDATE]);
  const [defaultModel, setDefaultModel] = useState("");
  const [shadow, setShadow] = useState("");
  // judge / webhook (also hybrid escalation)
  const [judgeModel, setJudgeModel] = useState("");
  const [charBudget, setCharBudget] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [webhookToken, setWebhookToken] = useState("");
  const [webhookTimeout, setWebhookTimeout] = useState("");
  const [escalation, setEscalation] = useState<"judge" | "webhook">("judge");
  // embeddings
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [routes, setRoutes] = useState<RouteRow[]>([EMPTY_ROUTE]);

  useEffect(() => {
    if (open) {
      setName("");
      setStrategy("complexity");
      setCandidates([EMPTY_CANDIDATE]);
      setDefaultModel("");
      setShadow("");
      setJudgeModel("");
      setCharBudget("");
      setWebhookUrl("");
      setWebhookToken("");
      setWebhookTimeout("");
      setEscalation("judge");
      setEmbeddingModel("");
      setRoutes([EMPTY_ROUTE]);
    }
  }, [open]);

  const models = useQuery({
    queryKey: ["team-models", teamId, "all"],
    queryFn: ({ signal }) => listAllModels(teamId, signal),
    enabled: open && teamId.length > 0,
  });
  const chatModels = useMemo(
    () => (models.data ?? []).filter((m) => m.type === "chat"),
    [models.data],
  );
  const embeddingModels = useMemo(
    () => (models.data ?? []).filter((m) => m.type === "embeddings"),
    [models.data],
  );
  const candidateNames = candidates.map((c) => c.modelName).filter(Boolean);

  const needsJudge = strategy === "judge" || (strategy === "hybrid" && escalation === "judge");
  const needsWebhook =
    strategy === "webhook" || (strategy === "hybrid" && escalation === "webhook");

  function buildStrategyConfig(): Record<string, unknown> {
    const judgeConfig: Record<string, unknown> = { judge_model: judgeModel };
    const budget = parsePositive(charBudget);
    if (budget !== null) judgeConfig.char_budget = budget;
    const webhookConfig: Record<string, unknown> = { url: webhookUrl.trim() };
    if (webhookToken.trim()) webhookConfig.bearer_token = webhookToken.trim();
    const timeout = parsePositive(webhookTimeout);
    if (timeout !== null) webhookConfig.timeout_ms = timeout;

    switch (strategy) {
      case "complexity":
      case "weighted":
        return {};
      case "judge":
        return judgeConfig;
      case "webhook":
        return webhookConfig;
      case "hybrid":
        return {
          escalation_strategy: escalation,
          escalation: escalation === "judge" ? judgeConfig : webhookConfig,
        };
      case "embeddings":
        return {
          embedding_model: embeddingModel,
          routes: routes.map((r) => ({
            name: r.name.trim(),
            target_model: r.targetModel,
            utterances: r.utterances
              .split("\n")
              .map((u) => u.trim())
              .filter(Boolean),
            ...(parseThreshold(r.threshold) !== null
              ? { threshold: parseThreshold(r.threshold) }
              : {}),
          })),
        };
    }
  }

  const mutation = useMutation({
    mutationFn: () => {
      const body = {
        name: name.trim(),
        candidates: candidates.map(
          (c): CandidateRequest => ({
            model_name: c.modelName,
            description: c.description.trim(),
            quality_tier: c.tier,
            supports_vision: c.vision,
            supports_tools: c.tools,
            supports_json_schema: c.jsonSchema,
            ...(strategy === "weighted" && parsePositive(c.weight) !== null
              ? { weight: parsePositive(c.weight) }
              : {}),
          }),
        ),
        default_model: defaultModel,
        strategy,
        strategy_config: buildStrategyConfig(),
        enabled: true,
        shadow_strategy: shadow || null,
      };
      return createRouter(teamId, body);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["team-routers", teamId] });
      onOpenChange(false);
    },
  });

  const candidatesValid =
    candidates.length > 0 &&
    candidates.every((c) => c.modelName.length > 0 && c.description.trim().length > 0);
  const strategyValid =
    (!needsJudge || judgeModel.length > 0) &&
    (!needsWebhook || webhookUrl.trim().startsWith("http")) &&
    (strategy !== "embeddings" ||
      (embeddingModel.length > 0 &&
        routes.length > 0 &&
        routes.every(
          (r) =>
            r.name.trim() &&
            r.targetModel &&
            r.utterances.trim().length > 0 &&
            thresholdValid(r.threshold),
        )));
  const canSubmit =
    !mutation.isPending &&
    name.trim().length > 0 &&
    candidatesValid &&
    candidateNames.includes(defaultModel) &&
    strategyValid;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSubmit) mutation.mutate();
  }

  function patchCandidate(index: number, patch: Partial<CandidateRow>) {
    setCandidates((prev) => prev.map((c, i) => (i === index ? { ...c, ...patch } : c)));
  }

  function patchRoute(index: number, patch: Partial<RouteRow>) {
    setRoutes((prev) => prev.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) mutation.reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>New router</DialogTitle>
          <DialogDescription>
            A virtual model: clients call it by name, the strategy routes each request to one of
            the candidate models.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="router-name">name</Label>
            <Input
              id="router-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. smart-chat"
              autoComplete="off"
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="router-strategy">strategy</Label>
            <select
              id="router-strategy"
              className={SELECT_CLASS}
              value={strategy}
              onChange={(e) => setStrategy(e.target.value as StrategyId)}
            >
              {STRATEGY_IDS.map((s) => (
                <option key={s} value={s}>
                  {STRATEGY_LABELS[s]}
                </option>
              ))}
            </select>
            <p className="font-mono text-xs text-muted-foreground">{STRATEGY_HELP[strategy]}</p>
          </div>

          {/* Candidates */}
          <div className="grid gap-2">
            <div className="flex items-center justify-between">
              <Label>candidates</Label>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setCandidates((prev) => [...prev, EMPTY_CANDIDATE])}
              >
                <Plus className="h-3.5 w-3.5" />
                add
              </Button>
            </div>
            {candidates.map((candidate, index) => (
              <div key={index} className="grid gap-2 rounded-md border border-border p-3">
                <div className="flex items-center gap-2">
                  <select
                    className={SELECT_CLASS}
                    aria-label={`candidate ${index + 1} model`}
                    value={candidate.modelName}
                    onChange={(e) => patchCandidate(index, { modelName: e.target.value })}
                  >
                    <option value="" disabled>
                      {models.isLoading
                        ? "loading…"
                        : chatModels.length === 0
                          ? "no chat models on this team"
                          : "select a model"}
                    </option>
                    {chatModels.map((m) => (
                      <option key={m.id} value={m.name}>
                        {m.name}
                      </option>
                    ))}
                  </select>
                  <select
                    className={SELECT_CLASS + " max-w-40"}
                    aria-label={`candidate ${index + 1} tier`}
                    value={candidate.tier}
                    onChange={(e) => patchCandidate(index, { tier: e.target.value as QualityTier })}
                  >
                    {QUALITY_TIERS.map((tier) => (
                      <option key={tier} value={tier}>
                        {tier.toLowerCase()}
                      </option>
                    ))}
                  </select>
                  {strategy === "weighted" ? (
                    <Input
                      type="number"
                      min="1"
                      className="max-w-24"
                      aria-label={`candidate ${index + 1} weight`}
                      placeholder="weight"
                      value={candidate.weight}
                      onChange={(e) => patchCandidate(index, { weight: e.target.value })}
                    />
                  ) : null}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 shrink-0 text-muted-foreground hover:text-destructive"
                    aria-label={`remove candidate ${index + 1}`}
                    disabled={candidates.length === 1}
                    onClick={() => setCandidates((prev) => prev.filter((_, i) => i !== index))}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
                <Input
                  aria-label={`candidate ${index + 1} description`}
                  placeholder="what this model is good at (used by judge/webhook strategies)"
                  value={candidate.description}
                  onChange={(e) => patchCandidate(index, { description: e.target.value })}
                  autoComplete="off"
                />
                <div className="flex gap-4 font-mono text-xs text-muted-foreground">
                  {(
                    [
                      ["vision", candidate.vision],
                      ["tools", candidate.tools],
                      ["json_schema", candidate.jsonSchema],
                    ] as const
                  ).map(([flag, value]) => (
                    <label key={flag} className="flex cursor-pointer items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={value}
                        onChange={(e) =>
                          patchCandidate(index, {
                            [flag === "json_schema" ? "jsonSchema" : flag]: e.target.checked,
                          } as Partial<CandidateRow>)
                        }
                      />
                      {flag}
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <div className="grid gap-2">
            <Label htmlFor="router-default">default model</Label>
            <select
              id="router-default"
              className={SELECT_CLASS}
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
            >
              <option value="" disabled>
                {candidateNames.length === 0 ? "add a candidate first" : "select the fallback"}
              </option>
              {candidateNames.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
            <p className="font-mono text-xs text-muted-foreground">
              Must be one of the candidates — any strategy failure routes here.
            </p>
          </div>

          {/* Strategy-specific config */}
          {strategy === "hybrid" ? (
            <div className="grid gap-2">
              <Label htmlFor="router-escalation">escalation strategy</Label>
              <select
                id="router-escalation"
                className={SELECT_CLASS}
                value={escalation}
                onChange={(e) => setEscalation(e.target.value as "judge" | "webhook")}
              >
                <option value="judge">judge</option>
                <option value="webhook">webhook</option>
              </select>
            </div>
          ) : null}
          {needsJudge ? (
            <div className="grid grid-cols-2 gap-3">
              <div className="grid gap-2">
                <Label htmlFor="router-judge">judge model</Label>
                <select
                  id="router-judge"
                  className={SELECT_CLASS}
                  value={judgeModel}
                  onChange={(e) => setJudgeModel(e.target.value)}
                >
                  <option value="" disabled>
                    {chatModels.length === 0 ? "no chat models" : "select a chat model"}
                  </option>
                  {chatModels.map((m) => (
                    <option key={m.id} value={m.name}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="router-char-budget">
                  char budget<span className="ml-1 text-muted-foreground">(opt)</span>
                </Label>
                <Input
                  id="router-char-budget"
                  type="number"
                  min="1"
                  value={charBudget}
                  onChange={(e) => setCharBudget(e.target.value)}
                  placeholder="prompt chars sent to the judge"
                  autoComplete="off"
                />
              </div>
            </div>
          ) : null}
          {needsWebhook ? (
            <div className="grid gap-3">
              <div className="grid gap-2">
                <Label htmlFor="router-webhook-url">webhook url</Label>
                <Input
                  id="router-webhook-url"
                  value={webhookUrl}
                  onChange={(e) => setWebhookUrl(e.target.value)}
                  placeholder="https://router.example.com/decide"
                  autoComplete="off"
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="grid gap-2">
                  <Label htmlFor="router-webhook-token">
                    bearer token<span className="ml-1 text-muted-foreground">(opt)</span>
                  </Label>
                  <Input
                    id="router-webhook-token"
                    type="password"
                    value={webhookToken}
                    onChange={(e) => setWebhookToken(e.target.value)}
                    autoComplete="off"
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="router-webhook-timeout">
                    timeout ms<span className="ml-1 text-muted-foreground">(opt)</span>
                  </Label>
                  <Input
                    id="router-webhook-timeout"
                    type="number"
                    min="1"
                    value={webhookTimeout}
                    onChange={(e) => setWebhookTimeout(e.target.value)}
                    placeholder="2000"
                    autoComplete="off"
                  />
                </div>
              </div>
            </div>
          ) : null}
          {strategy === "embeddings" ? (
            <div className="grid gap-2">
              <div className="grid gap-2">
                <Label htmlFor="router-embedding-model">embedding model</Label>
                <select
                  id="router-embedding-model"
                  className={SELECT_CLASS}
                  value={embeddingModel}
                  onChange={(e) => setEmbeddingModel(e.target.value)}
                >
                  <option value="" disabled>
                    {embeddingModels.length === 0
                      ? "no embeddings models on this team"
                      : "select an embeddings model"}
                  </option>
                  {embeddingModels.map((m) => (
                    <option key={m.id} value={m.name}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="flex items-center justify-between">
                <Label>routes</Label>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setRoutes((prev) => [...prev, EMPTY_ROUTE])}
                >
                  <Plus className="h-3.5 w-3.5" />
                  add
                </Button>
              </div>
              {routes.map((route, index) => (
                <div key={index} className="grid gap-2 rounded-md border border-border p-3">
                  <div className="flex items-center gap-2">
                    <Input
                      aria-label={`route ${index + 1} name`}
                      placeholder="route name, e.g. code"
                      value={route.name}
                      onChange={(e) => patchRoute(index, { name: e.target.value })}
                      autoComplete="off"
                    />
                    <select
                      className={SELECT_CLASS}
                      aria-label={`route ${index + 1} target model`}
                      value={route.targetModel}
                      onChange={(e) => patchRoute(index, { targetModel: e.target.value })}
                    >
                      <option value="" disabled>
                        {candidateNames.length === 0 ? "add candidates first" : "target model"}
                      </option>
                      {candidateNames.map((n) => (
                        <option key={n} value={n}>
                          {n}
                        </option>
                      ))}
                    </select>
                    <Input
                      type="number"
                      step="any"
                      min="0"
                      max="1"
                      className="max-w-28"
                      aria-label={`route ${index + 1} threshold`}
                      placeholder="threshold"
                      value={route.threshold}
                      onChange={(e) => patchRoute(index, { threshold: e.target.value })}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 shrink-0 text-muted-foreground hover:text-destructive"
                      aria-label={`remove route ${index + 1}`}
                      disabled={routes.length === 1}
                      onClick={() => setRoutes((prev) => prev.filter((_, i) => i !== index))}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                  <textarea
                    className={SELECT_CLASS + " h-20 py-2"}
                    aria-label={`route ${index + 1} utterances`}
                    placeholder={"example utterances, one per line"}
                    value={route.utterances}
                    onChange={(e) => patchRoute(index, { utterances: e.target.value })}
                  />
                  {thresholdValid(route.threshold) ? null : (
                    <p className="font-mono text-xs text-destructive">
                      threshold must be between 0 and 1 (0 excluded) — leave blank for the default
                      0.80
                    </p>
                  )}
                </div>
              ))}
            </div>
          ) : null}

          <div className="grid gap-2">
            <Label htmlFor="router-shadow">
              shadow strategy<span className="ml-1 text-muted-foreground">(optional)</span>
            </Label>
            <select
              id="router-shadow"
              className={SELECT_CLASS}
              value={shadow}
              onChange={(e) => setShadow(e.target.value)}
            >
              <option value="">none</option>
              {SHADOWABLE_STRATEGIES.filter((s) => s !== strategy).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <p className="font-mono text-xs text-muted-foreground">
              Runs alongside the live strategy and records what it would have chosen — nothing is
              routed differently.
            </p>
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
