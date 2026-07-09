import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";

export type Organization = components["schemas"]["OrganizationResponse"];

/** GET /organizations — platform-admin list (offset-paginated, stable order). */
export async function listOrganizations(): Promise<Organization[]> {
  const { data, error } = await api.GET("/organizations");
  if (error || !data) {
    throw new Error("Failed to load organizations");
  }
  return data;
}
