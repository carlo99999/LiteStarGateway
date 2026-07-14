import createClient, { type Middleware } from "openapi-fetch";
import { config } from "@/lib/config";
import type { paths } from "@/lib/api/schema";
import { csrfHeaderValue } from "@/lib/api/csrf";

/**
 * Typed API client generated from the gateway's OpenAPI schema.
 *
 * The browser session JWT remains in an HttpOnly same-origin cookie. Only the
 * non-secret CSRF value is held in memory and attached to unsafe requests.
 */
let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    const value = csrfHeaderValue(request.method, csrfToken);
    if (value) request.headers.set("X-CSRF-Token", value);
    return request;
  },
};

export const api = createClient<paths>({
  baseUrl: config.apiBaseUrl,
  credentials: "same-origin",
});
api.use(authMiddleware);
