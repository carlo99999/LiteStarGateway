import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// The E2E "deterministic test app": a fresh SQLite file per run plus a fixed
// MASTER_KEY/ADMIN_EMAIL. The bootstrap hook (src/litestar_gateway/infrastructure/
// bootstrap.py) creates exactly one admin user, `ensure_admin`, from those two
// values the first time the app boots against an empty database — the same
// mechanism `just docker-ci` uses, just without Docker. No other seed script is
// needed: the specs themselves create the team/key/org fixtures they exercise,
// which doubles as coverage for those creation flows.
export const E2E_ADMIN_EMAIL = "e2e-admin@example.com";
export const E2E_ADMIN_PASSWORD = "e2e-master-key-not-a-secret"; // pragma: allowlist secret

// An uncommon port: 8000 collides with Docker Desktop's proxy and with the
// `just docker-ci`/`just test-postgres` recipes other slices/agents may have
// running locally at the same time, which silently serves 404s here since
// Playwright's health check would pass against the wrong service.
const PORT = 8934;
const BASE_URL = `http://127.0.0.1:${PORT}/ui/`;

// This config lives in ui/; the backend and its `uv` environment live at the
// repo root one directory up.
const REPO_ROOT = path.resolve(__dirname, "..");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // A handful of critical flows against one deterministic backend instance —
  // sequential workers avoid the seeded fixtures (team/org names) colliding.
  workers: 1,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "html",
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // Boots the real backend (uvicorn, production-style server) against a fresh
  // throwaway SQLite database, serving the already-built admin console at /ui
  // (see src/litestar_gateway/infrastructure/web/ui_site.py) — the same single-
  // process topology the container image uses. Requires `pnpm run build` to
  // have produced ui/dist first (the "run after UI build" step in CI).
  webServer: {
    // Wipe any stale database from a previous run first — every run starts
    // from a genuinely empty schema so `ensure_admin` always fires.
    command:
      `rm -rf .e2e-data && mkdir -p .e2e-data && ` +
      `uv run uvicorn litestar_gateway.app:app --host 127.0.0.1 --port ${PORT}`,
    cwd: REPO_ROOT,
    url: `http://127.0.0.1:${PORT}/health`,
    // Always start fresh: an uncommon port makes accidental reuse unlikely,
    // but the whole point of this bootstrap is a genuinely empty database —
    // reusing a leftover process would silently defeat that.
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      ENVIRONMENT: "development",
      DATABASE_URL: `sqlite+aiosqlite:///${path.join(REPO_ROOT, ".e2e-data", "e2e.db")}`,
      MASTER_KEY: E2E_ADMIN_PASSWORD,
      ADMIN_EMAIL: E2E_ADMIN_EMAIL,
      SALT_KEY: "e2e-salt-key-not-a-secret-either", // pragma: allowlist secret
    },
  },
});
