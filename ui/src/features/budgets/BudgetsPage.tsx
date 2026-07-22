import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  BUDGET_WINDOWS,
  deleteTeamBudget,
  setTeamBudget,
  type BudgetWindow,
} from "@/features/budgets/api";
import { canReadUsage } from "@/features/teams/access";
import { getTeamBudget } from "@/features/teams/api";
import { useAccessibleTeams } from "@/features/teams/useAccessibleTeams";
import { toError } from "@/lib/toError";

const SELECT_CLASS =
  "flex h-9 rounded-md border border-input bg-background px-3 py-1 font-mono text-sm " +
  "text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-background";

function formatUsd(cost: number): string {
  return `$${cost.toFixed(2)}`;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="font-mono text-[10px] uppercase tracking-widest text-muted-foreground">
        // {label}
      </p>
      <p className="tabular mt-2 text-2xl text-foreground">{value}</p>
    </div>
  );
}

/** Per-team spend caps: view the current budget (spent/remaining), set or
 * update the cap, or remove it. Enforcement happens at request admission. */
export function BudgetsPage() {
  const queryClient = useQueryClient();
  // BILLING_VIEWER and the platform auditor hold budget:read wherever they
  // hold usage:read (domain/authorization.py), so the same filter applies.
  const teams = useAccessibleTeams(canReadUsage);
  const [teamId, setTeamId] = useState("");
  const budget = useQuery({
    queryKey: ["teams", teamId, "budget"],
    queryFn: () => getTeamBudget(teamId),
    enabled: teamId.length > 0,
  });

  const [limitText, setLimitText] = useState("");
  const [window_, setWindow] = useState<BudgetWindow>("monthly");

  useEffect(() => {
    if (!teamId && teams.data?.length) setTeamId(teams.data[0].id);
  }, [teamId, teams.data]);
  // Prefill the form from the current budget whenever team/budget changes.
  useEffect(() => {
    setLimitText(budget.data ? String(budget.data.limit_cost) : "");
    setWindow(budget.data?.window === "daily" ? "daily" : "monthly");
  }, [budget.data, teamId]);

  const save = useMutation({
    mutationFn: () => setTeamBudget(teamId, Number(limitText), window_),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["teams", teamId, "budget"] }),
  });
  const remove = useMutation({
    mutationFn: () => deleteTeamBudget(teamId),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["teams", teamId, "budget"] }),
  });

  const limit = Number(limitText);
  const canSave =
    !save.isPending && teamId.length > 0 && Number.isFinite(limit) && limit > 0;
  const pending = save.isPending || remove.isPending;
  const spentPct =
    budget.data && budget.data.limit_cost > 0
      ? Math.min(100, Math.round((budget.data.spent / budget.data.limit_cost) * 100))
      : 0;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSave) save.mutate();
  }

  return (
    <>
      <PageHeader
        command="budgets list"
        title="Budgets"
        description="Per-team spend caps, enforced when requests are admitted. Select a team to view or change its budget."
      />
      <div className="mb-4 flex items-center gap-3">
        <Label htmlFor="budget-team">team</Label>
        <select
          id="budget-team"
          className={SELECT_CLASS + " min-w-64"}
          value={teamId}
          onChange={(event) => setTeamId(event.target.value)}
          disabled={teams.isLoading || teams.isError}
        >
          <option value="" disabled>
            {teams.isLoading
              ? "loading…"
              : teams.isError
                ? "failed to load teams"
                : "select a team"}
          </option>
          {(teams.data ?? []).map((team) => (
            <option key={team.id} value={team.id}>
              {team.name}
            </option>
          ))}
        </select>
      </div>

      {budget.data ? (
        <>
          <div className="mb-4 grid gap-3 sm:grid-cols-3">
            <Stat label={`cap · ${budget.data.window}`} value={formatUsd(budget.data.limit_cost)} />
            <Stat label="spent" value={formatUsd(budget.data.spent)} />
            <Stat label="remaining" value={formatUsd(budget.data.remaining)} />
          </div>
          <div className="mb-6">
            <div className="h-2 overflow-hidden rounded bg-secondary">
              <div
                className={`h-full rounded ${spentPct >= 90 ? "bg-destructive" : "bg-primary/60"}`}
                style={{ width: `${spentPct}%` }}
              />
            </div>
            <p className="mt-1 font-mono text-xs text-muted-foreground">
              {spentPct}% of the {budget.data.window} window used
            </p>
          </div>
        </>
      ) : budget.isError ? (
        <p className="mb-6 font-mono text-xs text-destructive">
          ! couldn&apos;t load the budget — {toError(budget.error)?.message}
        </p>
      ) : teamId && !budget.isLoading ? (
        <p className="mb-6 font-mono text-xs text-muted-foreground">
          // no budget configured — this team's spend is unlimited.
        </p>
      ) : null}

      <form onSubmit={handleSubmit} className="flex flex-wrap items-end gap-3">
        <div className="grid gap-2">
          <Label htmlFor="budget-limit">cap (usd)</Label>
          <Input
            id="budget-limit"
            type="number"
            step="any"
            min="0"
            className="w-40"
            value={limitText}
            onChange={(e) => setLimitText(e.target.value)}
            placeholder="e.g. 100"
            autoComplete="off"
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="budget-window">window</Label>
          <select
            id="budget-window"
            className={SELECT_CLASS + " w-36"}
            value={window_}
            onChange={(e) => setWindow(e.target.value as BudgetWindow)}
          >
            {BUDGET_WINDOWS.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" size="sm" disabled={!canSave || pending}>
          {save.isPending ? "saving…" : budget.data ? "update budget" : "set budget"}
        </Button>
        {budget.data ? (
          <Button
            type="button"
            size="sm"
            variant="destructive"
            disabled={pending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? "removing…" : "remove"}
          </Button>
        ) : null}
      </form>
      {save.isError ? (
        <p className="mt-2 font-mono text-xs text-destructive">{save.error.message}</p>
      ) : null}
      {remove.isError ? (
        <p className="mt-2 font-mono text-xs text-destructive">{remove.error.message}</p>
      ) : null}
    </>
  );
}
