import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageResult } from "@/lib/api/pagination";

export type User = components["schemas"]["UserResponse"];
export type Invite = components["schemas"]["InviteResponse"];
export type TeamRole = components["schemas"]["TeamRole"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /users — one platform-admin table page. */
export async function listUsersPage(offset: number): Promise<PageResult<User>> {
  const request = pageRequest(offset);
  const { data, error } = await api.GET("/users", { params: { query: request } });
  if (error || !data) throw fail(error, "Failed to load users");
  return pageResult(data, offset);
}

/** POST /invites — mint a single-use invite that joins the new account to
 * `teamId` with `role`. The token is returned once and never again. */
export async function inviteUser(teamId: string, role: TeamRole): Promise<Invite> {
  const { data, error } = await api.POST("/invites", {
    body: { team_id: teamId, role },
  });
  if (error || !data) throw fail(error, "Failed to create invite");
  return data;
}

/** PATCH /users/{id}/admin — grant or revoke the platform-admin role. */
export async function setUserAdmin(userId: string, isAdmin: boolean): Promise<User> {
  const { data, error } = await api.PATCH("/users/{user_id}/admin", {
    params: { path: { user_id: userId } },
    body: { is_admin: isAdmin },
  });
  if (error || !data) throw fail(error, "Failed to change admin role");
  return data;
}

/** PATCH /users/{id}/auditor — grant or revoke the read-only auditor role. */
export async function setUserAuditor(userId: string, isAuditor: boolean): Promise<User> {
  const { data, error } = await api.PATCH("/users/{user_id}/auditor", {
    params: { path: { user_id: userId } },
    body: { is_auditor: isAuditor },
  });
  if (error || !data) throw fail(error, "Failed to change auditor role");
  return data;
}

/** PATCH /users/{id} — enable or disable the account. Disabling also revokes the
 * user's existing sessions. */
export async function setUserActive(userId: string, isActive: boolean): Promise<User> {
  const { data, error } = await api.PATCH("/users/{user_id}", {
    params: { path: { user_id: userId } },
    body: { is_active: isActive },
  });
  if (error || !data) throw fail(error, "Failed to change account status");
  return data;
}

/** DELETE /users/{id}/lock — lift a login lockout (admin action). */
export async function unlockUser(userId: string): Promise<void> {
  const { error } = await api.DELETE("/users/{user_id}/lock", {
    params: { path: { user_id: userId } },
  });
  if (error) throw fail(error, "Failed to unlock account");
}

/** DELETE /users/{id} — hard-delete a user. Refused (409) while the account
 * still has team memberships or API keys it created. */
export async function deleteUser(userId: string): Promise<void> {
  const { error } = await api.DELETE("/users/{user_id}", {
    params: { path: { user_id: userId } },
  });
  if (error) throw fail(error, "Failed to delete user");
}
