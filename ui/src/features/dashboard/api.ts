import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type MyTeam = components["schemas"]["MyTeamResponse"];

/** Shape of the savings aggregates (untyped dicts in the OpenAPI schema). */
export interface SavingsSummary {
  estimated_savings: number;
  decisions_counted: number;
  decisions_without_usage: number;
}

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /me/teams — the caller's own team memberships (any authenticated user). */
export async function listMyTeams(): Promise<MyTeam[]> {
  const { data, error } = await api.GET("/me/teams");
  if (error || !data) throw fail(error, "Failed to load your teams");
  return data;
}

/** GET /routing/savings — smart-routing savings across the whole platform
 * (platform-admin only). */
export async function platformSavings(): Promise<SavingsSummary> {
  const { data, error } = await api.GET("/routing/savings");
  if (error || !data) throw fail(error, "Failed to load routing savings");
  return data as unknown as SavingsSummary;
}

/** GET /teams/{id}/savings — one team's smart-routing savings (team usage:read;
 * plain members are refused by design — callers treat a 403 as "not visible"). */
export async function teamSavings(teamId: string): Promise<SavingsSummary> {
  const { data, error } = await api.GET("/teams/{team_id}/savings", {
    params: { path: { team_id: teamId } },
  });
  if (error || !data) throw fail(error, "Failed to load team savings");
  return data as unknown as SavingsSummary;
}
