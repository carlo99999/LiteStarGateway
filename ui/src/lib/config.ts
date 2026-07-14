/**
 * Runtime configuration for the admin console.
 *
 * The UI is served under `/ui/` but the gateway REST API lives at the root
 * (`/login`, `/organizations`, ...). In development, Vite proxies every
 * non-`/ui` path to the gateway, so the default API base URL is the empty
 * string (same-origin, root-relative). Browser sessions intentionally require
 * the console and API to share an origin; dev uses the Vite proxy.
 */
export const config = {
  apiBaseUrl: "",
  /** Removal-only key for JWTs persisted by releases before cookie sessions. */
  legacyTokenStorageKey: "lsg.admin.token",
} as const;
