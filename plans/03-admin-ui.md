# Plan 03 — Admin UI

**Design doc:** none yet (write a short one before phase 1 if the surface grows).
**Depends on:** backend only — the REST surface already exists; `ui/` is currently
empty.
**Theme:** a React/Vite admin console so non-developers can operate the gateway
(teams, budgets, keys, usage) without curl.

## Scope

Thin admin client over the existing controllers under `infrastructure/web/`:

- **Auth:** login (JWT), session handling, logout. Reuse the existing
  `/login` + bearer-token flow; SSO where already supported.
- **Organizations & teams:** list/create; per-team members and roles (the extended
  RBAC — admin / model-manager / key-issuer / billing-viewer).
- **Models & credentials:** list/create/enable-disable models; manage credentials
  (never display secret material — the API already redacts it).
- **API keys / service principals:** issue, scope, revoke; show prefix only.
- **Budgets:** view/set per-team spend caps and current spend.
- **Usage & cost:** per-team/per-key usage and cost views (the observability data
  the backend already records).
- **Routing (read-first):** view routers and recent routing decisions; editing is a
  later increment.

## Phases

### Phase 0 — Scaffold

- Vite + React + TypeScript in `ui/`; a typed API client generated from or aligned
  to the backend's OpenAPI schema (Litestar serves one) — single source of truth
  for request/response shapes.
- Auth context + protected routes; error/permission handling that mirrors the API's
  status codes (401/403/409/429).
- Dev proxy to the gateway; a `just ui-dev` recipe.
- **Done when:** login works against a locally-running gateway and an authed shell
  renders.

### Phase 1 — Read-only console

- All list/detail views above, read-only. Proves the schema client, auth, RBAC-aware
  rendering (hide what the caller can't see), and pagination (the API is offset-
  paginated with stable ordering after L36).
- **Done when:** an admin can see teams, members, models, keys, budgets, and usage.

### Phase 2 — Mutations

- Create/update/revoke flows for teams, members, models, credentials, keys, budgets.
- Optimistic-but-verified UX; every mutation surfaces the API's domain errors
  (e.g. `CredentialInUse` 409, `LastTeamAdmin`) as clear messages.

### Phase 3 — Packaging

- Build the SPA and serve it (static mount or a separate container in
  `docker-compose.yml`); decide auth-cookie vs bearer for the browser context.
- Add a UI build/lint job to CI (path-gated to `ui/`).

## Conventions

- Match the repo's coding-style rules on the frontend too: many small files,
  immutable state updates, schema-based validation at the boundary, explicit error
  handling, no secrets in client code.
- **Never render secret material** — the API redacts credentials and shows key
  prefixes only; the UI must not reconstruct or request full secrets.

## Risks & mitigations

- **Schema drift** → generate the client from the backend OpenAPI schema and fail CI
  if it diverges, rather than hand-maintaining types.
- **RBAC leakage in the UI** → treat the API as the source of truth; the UI hides
  affordances for convenience but the server still enforces every permission.

## Execution

Fully backend-independent → can run in a parallel track whenever capacity allows,
without blocking Plans 01/02. Phase 0 is one slice; phases 1–2 slice naturally by
resource (teams, models, keys, budgets, usage) across worktrees.
