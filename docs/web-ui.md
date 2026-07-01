# Design doc — Web UI (post-v1)

> **Status:** Draft / parked. Lives on branch `adding-web-ui`. Deliberately
> **after the gateway reaches v1** — the API and auth are the product; the UI is
> a client on top. No code yet.

## 1. Goal

A web UI for humans to do what the JSON API already exposes: log in, manage
organizations / teams / members, credentials, models, and API keys, and (later)
see usage/cost dashboards. The 7-day **JWT login** was designed for exactly this.

## 2. Scope (v1 of the UI)

- **Auth**: login (email + password → JWT), logout, "me". Store the JWT
  client-side; send it as `Authorization: Bearer`.
- **Platform-admin screens**: organizations, teams (+ first-admin), credentials
  (write-only secrets — never displayed), models.
- **Team-admin screens**: team members (add/remove/roles), team models, team API
  keys (show the plaintext key **once** at creation).
- **Read-only usage** (once observability lands): per-team/model spend and volume.

Out of scope for UI v1: the routing features (weighted/smart) UIs — add once those
backends exist.

## 3. Architecture

- **Separate SPA** (React or Svelte) talking to the existing JSON API — do **not**
  couple it into the Python app. The gateway stays a pure API; the UI is an
  independent build artifact.
- Served either fully separately (its own host/CDN) or as static files behind a
  Litestar static route. Decision below.
- No new backend concepts required: the UI is a client of endpoints that already
  exist. Small backend additions likely needed: a `GET /v1/models`-style listing
  and list endpoints for orgs/teams the UI can page through.

## 4. Cross-cutting concerns

- **CORS**: the API needs a CORS config allowing the UI origin (currently none).
- **Token lifetime/refresh**: 7-day JWT with no refresh today; UI must handle
  401 → redirect to login. Consider a refresh-token flow later.
- **Secret display**: credentials' secrets are write-only; the UI must reflect
  that (fields are set-only, never read back). API keys shown once.
- **RBAC in the UI** mirrors the API (platform-admin vs team-admin vs member) —
  but the API remains the source of truth; the UI only hides what the API forbids.

## 5. Open decisions

1. **Framework**: React vs Svelte (team preference).
2. **Hosting**: standalone SPA (separate origin + CORS) vs static files served by
   Litestar (same origin, no CORS). Lean: standalone for a clean separation.
3. **Auth storage**: in-memory + refresh vs cookie (httpOnly). Lean: httpOnly
   cookie or short-lived token + refresh; revisit the 7-day-JWT model for a UI.
4. **Usage dashboards** depend on the observability + usage/cost work landing
   first.

## 6. Prerequisites (why post-v1)

- Gateway feature-complete and stable at v1.
- Likely: `GET /v1/models` + list endpoints, a CORS config, and (for dashboards)
  the observability/usage work.

## 7. Rollout

1. `feat/ui-api-prep` — CORS config + listing endpoints the UI needs.
2. `feat/web-ui-auth` — SPA scaffold + login/logout/me.
3. `feat/web-ui-admin` — orgs/teams/members/credentials/models/keys screens.
4. `feat/web-ui-usage` — dashboards (after observability).
