import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import {
  fetchAllPages,
  pageRequest,
  pageResult,
  type PageRequest,
  type PageResult,
} from "@/lib/api/pagination";

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

/** Complete credentials collection, for selectors (e.g. the model create form). */
export async function listAllCredentials(signal?: AbortSignal): Promise<Credential[]> {
  return fetchAllPages(
    async (request) => {
      const { data, error } = await api.GET("/credentials", { params: { query: request }, signal });
      if (error || !data) throw fail(error, "Failed to load credentials");
      return data;
    },
    { keyOf: (credential) => credential.id },
  );
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

/** PATCH /credentials/{id} — rename and/or replace the secret values (e.g. a
 * rotated token). Pass `values` only to replace them (a full set — secrets are
 * never returned, so it can't merge); omit to keep the current ones. */
export async function updateCredential(
  id: string,
  changes: { name?: string; values?: Record<string, string> },
): Promise<Credential> {
  const { data, error } = await api.PATCH("/credentials/{credential_id}", {
    params: { path: { credential_id: id } },
    body: { name: changes.name ?? null, values: changes.values ?? null },
  });
  if (error || !data) throw fail(error, "Failed to update credential");
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
