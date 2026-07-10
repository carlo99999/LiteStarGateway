import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type Organization = components["schemas"]["OrganizationResponse"];
export type OrganizationSpend = components["schemas"]["OrganizationSpendResponse"];

/** Pull a human message out of the gateway's OpenAI-shaped error envelope. */
function errorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return env.error.message;
    if (env.detail) return env.detail;
  }
  return fallback;
}

/** GET /organizations — platform-admin list (offset-paginated, stable order). */
export async function listOrganizations(): Promise<Organization[]> {
  const { data, error } = await api.GET("/organizations");
  if (error || !data) {
    throw new Error(errorMessage(error, "Failed to load organizations"));
  }
  return data;
}

/** GET /organizations/{id} — one tenant (platform-admin). */
export async function getOrganization(id: string): Promise<Organization> {
  const { data, error } = await api.GET("/organizations/{organization_id}", {
    params: { path: { organization_id: id } },
  });
  if (error || !data) {
    throw new Error(errorMessage(error, "Failed to load organization"));
  }
  return data;
}

/** GET /organizations/{id}/spend — cost over the last `days`, summed across the
 * org's teams with a per-team breakdown (platform-admin). */
export async function getOrganizationSpend(
  id: string,
  days = 30,
): Promise<OrganizationSpend> {
  const { data, error } = await api.GET("/organizations/{organization_id}/spend", {
    params: { path: { organization_id: id }, query: { days } },
  });
  if (error || !data) {
    throw new Error(errorMessage(error, "Failed to load organization spend"));
  }
  return data;
}

/** POST /organizations — create a tenant (platform-admin). */
export async function createOrganization(name: string): Promise<Organization> {
  const { data, error } = await api.POST("/organizations", { body: { name } });
  if (error || !data) {
    throw new Error(errorMessage(error, "Failed to create organization"));
  }
  return data;
}

/** PATCH /organizations/{id} — rename a tenant (platform-admin). */
export async function updateOrganization(id: string, name: string): Promise<Organization> {
  const { data, error } = await api.PATCH("/organizations/{organization_id}", {
    params: { path: { organization_id: id } },
    body: { name },
  });
  if (error || !data) {
    throw new Error(errorMessage(error, "Failed to rename organization"));
  }
  return data;
}

/** DELETE /organizations/{id} — remove an empty tenant (platform-admin). The API
 * refuses (409) if the organization still has teams. */
export async function deleteOrganization(id: string): Promise<void> {
  const { error } = await api.DELETE("/organizations/{organization_id}", {
    params: { path: { organization_id: id } },
  });
  if (error) {
    throw new Error(errorMessage(error, "Failed to delete organization"));
  }
}
