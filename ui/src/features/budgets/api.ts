import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type TeamBudget = components["schemas"]["BudgetResponse"];
export type BudgetAlert = components["schemas"]["BudgetAlertResponse"];
export type BudgetWindow = "monthly" | "daily";

export const BUDGET_WINDOWS: BudgetWindow[] = ["monthly", "daily"];

/** Per-team alert config sent alongside the cap on PUT /budget (Plan 07
 * Phase 3). Omitted/empty fields clear the corresponding config. */
export interface BudgetAlertConfig {
  thresholds: number[];
  alertWebhookUrl: string | null;
  alertEmail: string | null;
}

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** PUT /teams/{id}/budget — set or replace the team's spend cap and its
 * alert config (thresholds + channel targets). */
export async function setTeamBudget(
  teamId: string,
  limitCost: number,
  window: BudgetWindow,
  alerts: BudgetAlertConfig,
): Promise<TeamBudget> {
  const { data, error } = await api.PUT("/teams/{team_id}/budget", {
    params: { path: { team_id: teamId } },
    body: {
      limit_cost: limitCost,
      window,
      thresholds: alerts.thresholds,
      alert_webhook_url: alerts.alertWebhookUrl,
      alert_email: alerts.alertEmail,
    },
  });
  if (error || !data) throw fail(error, "Failed to set budget");
  return data;
}

/** DELETE /teams/{id}/budget — remove the cap (spend becomes unlimited). */
export async function deleteTeamBudget(teamId: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}/budget", {
    params: { path: { team_id: teamId } },
  });
  if (error) throw fail(error, "Failed to remove budget");
}

/** GET /teams/{id}/budget/alerts — recent fired budget-threshold alerts,
 * newest-first. Read-gated the same as the budget itself (budget:read). */
export async function getBudgetAlerts(teamId: string): Promise<BudgetAlert[]> {
  const { data, error } = await api.GET("/teams/{team_id}/budget/alerts", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load alerts");
  return data;
}
