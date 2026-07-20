import { api } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import { pageRequest, pageResult, type PageResult } from "@/lib/api/pagination";

export type AuditEvent = components["schemas"]["AuditEventResponse"];

function fail(error: unknown, fallback: string): Error {
  if (error && typeof error === "object") {
    const env = error as { error?: { message?: string }; detail?: string };
    if (env.error?.message) return new Error(env.error.message);
    if (env.detail) return new Error(env.detail);
  }
  return new Error(fallback);
}

/** GET /audit — one page of recent audit events (most recent first). */
export async function listAuditPage(offset: number): Promise<PageResult<AuditEvent>> {
  const request = pageRequest(offset);
  const { data, error } = await api.GET("/audit", { params: { query: request } });
  if (error || !data) throw fail(error, "Failed to load audit events");
  return pageResult(data, offset);
}
