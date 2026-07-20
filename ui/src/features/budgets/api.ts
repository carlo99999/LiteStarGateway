import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type TeamBudget = components["schemas"]["BudgetResponse"];
export type BudgetWindow = "monthly" | "daily";

export const BUDGET_WINDOWS: BudgetWindow[] = ["monthly", "daily"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** PUT /teams/{id}/budget — set or replace the team's spend cap. */
export async function setTeamBudget(
  teamId: string,
  limitCost: number,
  window: BudgetWindow,
): Promise<TeamBudget> {
  const { data, error } = await api.PUT("/teams/{team_id}/budget", {
    params: { path: { team_id: teamId } },
    body: { limit_cost: limitCost, window },
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
