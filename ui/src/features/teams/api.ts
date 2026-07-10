import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type Team = components["schemas"]["TeamResponse"];
export type TeamMember = components["schemas"]["MembershipResponse"];
export type TeamBudget = components["schemas"]["BudgetResponse"];
export type TeamUsage = components["schemas"]["UsageResponse"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /teams — every team across all organizations (platform-admin). */
export async function listTeams(): Promise<Team[]> {
  const { data, error } = await api.GET("/teams");
  if (error || !data) throw fail(error, "Failed to load teams");
  return data;
}

/** GET /teams/{id} — one team. */
export async function getTeam(id: string): Promise<Team> {
  const { data, error } = await api.GET("/teams/{team_id}", {
    params: { path: { team_id: id } },
  });
  if (error || !data) throw fail(error, "Failed to load team");
  return data;
}

/** GET /teams/{id}/members — team membership list. */
export async function listTeamMembers(id: string): Promise<TeamMember[]> {
  const { data, error } = await api.GET("/teams/{team_id}/members", {
    params: { path: { team_id: id } },
  });
  if (error || !data) throw fail(error, "Failed to load members");
  return data;
}

/** GET /teams/{id}/budget — the team budget, or null when none is configured (404). */
export async function getTeamBudget(id: string): Promise<TeamBudget | null> {
  const { data, error } = await api.GET("/teams/{team_id}/budget", {
    params: { path: { team_id: id } },
  });
  if (error || !data) return null;
  return data;
}

/** GET /teams/{id}/usage — per-model token/cost totals for the team. */
export async function listTeamUsage(id: string): Promise<TeamUsage[]> {
  const { data, error } = await api.GET("/teams/{team_id}/usage", {
    params: { path: { team_id: id } },
  });
  if (error || !data) throw fail(error, "Failed to load usage");
  return data;
}

/** POST /organizations/{orgId}/teams — create a team (platform-admin). The
 * admin_email must be an existing user, who becomes the team's first admin. */
export async function createTeam(
  organizationId: string,
  name: string,
  adminEmail: string,
): Promise<Team> {
  const { data, error } = await api.POST("/organizations/{organization_id}/teams", {
    params: { path: { organization_id: organizationId } },
    body: { name, admin_email: adminEmail },
  });
  if (error || !data) throw fail(error, "Failed to create team");
  return data;
}

/** PATCH /teams/{id} — rename a team (platform-admin). */
export async function updateTeam(id: string, name: string): Promise<Team> {
  const { data, error } = await api.PATCH("/teams/{team_id}", {
    params: { path: { team_id: id } },
    body: { name },
  });
  if (error || !data) throw fail(error, "Failed to rename team");
  return data;
}

/** DELETE /teams/{id} — remove a team (platform-admin). Refused (409) if the
 * team still has models or API keys. */
export async function deleteTeam(id: string): Promise<void> {
  const { error } = await api.DELETE("/teams/{team_id}", {
    params: { path: { team_id: id } },
  });
  if (error) throw fail(error, "Failed to delete team");
}
