import createClient, { type Middleware } from "openapi-fetch";
import { config } from "@/lib/config";
import type { paths } from "@/lib/api/schema";

/**
 * Typed API client generated from the gateway's OpenAPI schema.
 *
 * A single mutable token holder lets the auth layer swap the bearer token
 * (login / logout / restore-from-storage) without recreating the client.
 */
let bearerToken: string | null = null;

export function setBearerToken(token: string | null): void {
  bearerToken = token;
}

const authMiddleware: Middleware = {
  async onRequest({ request }) {
    if (bearerToken) {
      request.headers.set("Authorization", `Bearer ${bearerToken}`);
    }
    return request;
  },
};

export const api = createClient<paths>({ baseUrl: config.apiBaseUrl });
api.use(authMiddleware);
