/**
 * Runtime configuration for the admin console.
 *
 * The UI is served under `/ui/` but the gateway REST API lives at the root
 * (`/login`, `/organizations`, ...). In development, Vite proxies every
 * non-`/ui` path to the gateway, so the default API base URL is the empty
 * string (same-origin, root-relative). Override with `VITE_API_BASE_URL` when
 * the API is hosted elsewhere.
 */
export const config = {
  apiBaseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
  /** localStorage key holding the bearer JWT for Phase 0. */
  tokenStorageKey: "lsg.admin.token",
} as const;
