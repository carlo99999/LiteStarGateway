import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageRequest, type PageResult } from "@/lib/api/pagination";

export type Credential = components["schemas"]["CredentialResponse"];
export type Provider = components["schemas"]["Provider"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

async function requestCredentials(request: PageRequest): Promise<Credential[]> {
  const { data, error } = await api.GET("/credentials", { params: { query: request } });
  if (error || !data) throw fail(error, "Failed to load credentials");
  return data;
}

/** GET /credentials — one table page (metadata only; secrets are never returned). */
export async function listCredentialsPage(offset: number): Promise<PageResult<Credential>> {
  const request = pageRequest(offset);
  return pageResult(await requestCredentials(request), offset);
}

/** POST /credentials — create a provider credential. `values` are encrypted at
 * rest and can never be read back. */
export async function createCredential(
  name: string,
  provider: Provider,
  values: Record<string, string>,
): Promise<Credential> {
  const { data, error } = await api.POST("/credentials", {
    body: { name, provider, values },
  });
  if (error || !data) throw fail(error, "Failed to create credential");
  return data;
}

/** DELETE /credentials/{id} — remove a credential. Refused (409) if any model
 * still references it. */
export async function deleteCredential(id: string): Promise<void> {
  const { error } = await api.DELETE("/credentials/{credential_id}", {
    params: { path: { credential_id: id } },
  });
  if (error) throw fail(error, "Failed to delete credential");
}
