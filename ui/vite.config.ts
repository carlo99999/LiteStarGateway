import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The gateway serves its own OpenAPI/Swagger UI at `/`, so the admin console is
// mounted under `/ui/`. `base` makes built assets resolve as `/ui/assets/...`.
// The typed API client still targets the gateway API root (`/`), NOT `/ui`.
const GATEWAY_URL = process.env.GATEWAY_URL ?? "http://localhost:8000";

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // Proxy every non-`/ui` path to the gateway so the app can call the API at
    // the root (`/login`, `/organizations`, `/me`, ...) during development.
    proxy: {
      "^/(?!ui/|@|src/|node_modules/).*": {
        target: GATEWAY_URL,
        // Cookie-session CSRF compares Origin to the public Host. Preserve the
        // browser-facing Vite host instead of rewriting it to the backend target.
        changeOrigin: false,
      },
    },
  },
});
