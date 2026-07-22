import { useMutation, useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useEffect, useState } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { StatusDot } from "@/components/common/StatusDot";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { listCallableModels } from "@/features/models/api";
import { comparePrompt } from "@/features/playground/api";
import { listCallableRouters } from "@/features/routing/api";
import { useAccessibleTeams } from "@/features/teams/useAccessibleTeams";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 min-w-64 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

function formatCost(cost: number | null | undefined): string {
  if (cost === null || cost === undefined) return "—";
  return `$${cost.toFixed(6)}`;
}

/** Compare one prompt across several of a team's chat models: response,
 * latency, tokens and estimated cost, side by side. Real (unmetered) calls. */
export function PlaygroundPage() {
  const teams = useAccessibleTeams();
  const [teamId, setTeamId] = useState("");
  const models = useQuery({
    queryKey: ["team-models", teamId, "callable"],
    queryFn: ({ signal }) => listCallableModels(teamId, signal),
    enabled: teamId.length > 0,
  });
  const routers = useQuery({
    queryKey: ["team-routers", teamId, "callable"],
    queryFn: () => listCallableRouters(teamId),
    enabled: teamId.length > 0,
  });

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [prompt, setPrompt] = useState("");
  const [maxTokens, setMaxTokens] = useState("");

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  useEffect(() => setSelected(new Set()), [teamId]);

  // Chat models + routers can be compared (both resolve to a chat completion).
  const options: { alias: string; kind: "model" | "router"; badge?: string }[] = [
    ...(models.data ?? [])
      .filter((c) => c.model.type === "chat" && c.model.enabled)
      .map((c) => ({
        alias: c.alias,
        kind: "model" as const,
        badge: c.origin !== "own" ? c.origin : undefined,
      })),
    ...(routers.data ?? [])
      .filter((c) => c.router.enabled)
      .map((c) => ({ alias: c.alias, kind: "router" as const })),
  ];

  const run = useMutation({
    mutationFn: () => {
      const max = maxTokens.trim() === "" ? null : Number(maxTokens);
      return comparePrompt(teamId, [...selected], prompt, Number.isFinite(max) ? max : null);
    },
  });

  function toggle(alias: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(alias)) next.delete(alias);
      else next.add(alias);
      return next;
    });
  }

  const canRun = !run.isPending && teamId.length > 0 && selected.size > 0 && prompt.trim().length > 0;

  return (
    <>
      <PageHeader
        command="playground compare"
        title="Playground"
        description="Send one prompt to several models at once and compare the responses, latency, tokens and cost. Real calls — not metered against usage or budgets."
      />

      <div className="mb-3 flex flex-wrap items-center gap-3">
        <Label htmlFor="pg-team">team</Label>
        <select
          id="pg-team"
          className={SELECT_CLASS}
          value={teamId}
          onChange={(e) => setTeamId(e.target.value)}
          disabled={teams.isLoading || teams.isError}
        >
          <option value="" disabled>
            {teams.isLoading ? "loading…" : teams.isError ? "failed to load teams" : "select a team"}
          </option>
          {(teams.data ?? []).map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>

      <div className="mb-3 grid gap-1.5">
        <Label>models to compare</Label>
        {options.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            {teamId ? "no enabled chat models or routers for this team" : "select a team"}
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {options.map((o) => (
              <label
                key={`${o.kind}:${o.alias}`}
                className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-2 py-1 hover:bg-muted"
              >
                <input
                  type="checkbox"
                  checked={selected.has(o.alias)}
                  onChange={() => toggle(o.alias)}
                />
                <span className="text-sm">{o.alias}</span>
                {o.kind === "router" ? (
                  <Badge variant="muted">router</Badge>
                ) : o.badge ? (
                  <Badge variant="accent">{o.badge}</Badge>
                ) : null}
              </label>
            ))}
          </div>
        )}
      </div>

      <div className="mb-3 grid gap-1.5">
        <Label htmlFor="pg-prompt">prompt</Label>
        <textarea
          id="pg-prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={4}
          placeholder="Ask the models something…"
          className="flex w-full rounded-md border border-input bg-background px-3 py-2 font-mono text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>

      <div className="mb-4 flex items-end gap-3">
        <div className="grid gap-1.5">
          <Label htmlFor="pg-max">
            max completion tokens<span className="ml-1 text-muted-foreground">(opt)</span>
          </Label>
          <input
            id="pg-max"
            type="number"
            min="1"
            value={maxTokens}
            onChange={(e) => setMaxTokens(e.target.value)}
            placeholder="default"
            className="flex h-9 w-40 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>
        <Button size="sm" disabled={!canRun} onClick={() => run.mutate()}>
          <Play className="h-4 w-4" />
          {run.isPending ? "running…" : `compare ${selected.size || ""}`}
        </Button>
      </div>

      {run.isError ? (
        <p className="mb-4 font-mono text-xs text-destructive">{toError(run.error)?.message}</p>
      ) : null}

      {run.data ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {run.data.map((r) => (
            <div key={r.model_name} className="flex flex-col rounded-lg border border-border p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="flex items-center gap-1.5 font-medium">
                  <StatusDot tone={r.ok ? "green" : "amber"} />
                  {r.model_name}
                  {r.chosen_model ? (
                    <span className="font-mono text-[10px] font-normal text-muted-foreground">
                      → {r.chosen_model}
                    </span>
                  ) : null}
                </span>
                <span className="tabular text-[10px] text-muted-foreground">
                  {r.latency_ms !== null && r.latency_ms !== undefined
                    ? `${Math.round(r.latency_ms)}ms`
                    : ""}
                </span>
              </div>
              {r.ok ? (
                <>
                  <p className="mb-2 max-h-64 flex-1 overflow-y-auto whitespace-pre-wrap text-sm">
                    {r.content ?? <span className="text-muted-foreground">(empty)</span>}
                  </p>
                  <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border pt-2 font-mono text-[10px] text-muted-foreground">
                    <span>
                      tok {r.prompt_tokens ?? "—"} / {r.completion_tokens ?? "—"}
                    </span>
                    <span>cost {formatCost(r.cost)}</span>
                  </div>
                </>
              ) : (
                <p className="font-mono text-xs text-destructive">{r.error}</p>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}
