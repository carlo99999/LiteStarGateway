# Plan 11 — Platform quality gates

**Design sources:** [`docs/logging.md`](../docs/logging.md) for correlation IDs,
[`docs/db-migrations.md`](../docs/db-migrations.md) for migration drift, and
[`plans/03-admin-ui.md`](03-admin-ui.md) for OpenAPI/browser quality.

**Depends on:** shipped logging, CI and admin console.

**Theme:** make requests traceable and release drift/user-flow regressions
impossible to merge unnoticed.

## Independent slices

### A — Request correlation ✅ (27 July 2026)

- Request-ID middleware/hook, trusted-proxy validation and response header.
- Bind to structlog and propagate to audit/usage/routing/trace records.
- Tests prove ID consistency and absence of secrets.

**Done** (#382): `RequestIDMiddleware`
(`src/litestar_gateway/infrastructure/web/request_id.py`, registered app-wide
in `app.py`) resolves/binds/echoes an `X-Request-ID` per request. Header name:
`X-Request-ID`, 1-128 chars, charset `[A-Za-z0-9_-]`. Trusted-proxy policy: an
inbound value is accepted verbatim only when the direct connecting peer is in
the new `Settings.trusted_proxy_ips` (`TRUSTED_PROXY_IPS`, comma-separated
IPs/CIDRs) **and** passes the length/charset check — mirroring
`FORWARDED_ALLOW_IPS` (the existing ASGI-server-level trusted-proxy concept in
`docs/operations.md`), which lives outside app code and so needed an
analogous, minimal app-level allowlist rather than a parallel mechanism.
Every other case (untrusted source, malformed value, absent header) generates
a fresh id.

Propagation is contextvar-based, not parameter-threaded: `current_request_id()`
(`src/litestar_gateway/request_context.py` — a top-level module importing only
`structlog`, no `litestar`/`infrastructure`) reads the id bound to
`structlog.contextvars` by the middleware. `TraceRecord`, `UsageEvent`,
`AuditEvent` and `RoutingDecisionRecord` each gained a nullable `request_id`
field, set via this accessor at their construction sites (`usage_meter.py`,
`routing/service.py`, `audit/recorder.py`, the SCIM/SSO audit-event
constructors) — no `UsageMeter`/`RouterService`/`CompletionService` signature
had to change, and the domain/application → infrastructure/litestar boundary
stayed intact. `GET /audit` also now returns `request_id`.

Migration `2026-07-27_add_request_correlation_id_columns_7750ec93d00f` adds a
nullable `request_id` column (no `server_default`) to `usage_event`,
`pending_usage_event`, `audit_event` and `routing_decision` — nullable because
historical rows and background-worker-originated records (key rotation,
budget-alert reconciler, shadow routing) genuinely have no request to tag.

Also verified (no rework needed): the design doc's already-shipped catch-all
500 handling — Litestar's default exception path (no `debug=True`,
`log_exceptions="always"`) already returns a generic body while logging full
detail server-side.

### B — CI drift gates ✅ (27 July 2026)

- Wire `just migration-check`.
- Regenerate OpenAPI + TypeScript schema into temporary outputs and diff.
- Add a Markdown link checker for roadmap/design docs.

**Done** (#384): the `checks` job now applies migrations to a fresh SQLite DB
and runs `just migration-check` (`litestar database check`, an autogenerate-diff
gate) — fails on any ORM change shipped without a migration. A new
`schema-drift` CI job + `just ui-schema-check` regenerate `ui/openapi.json` and
`ui/src/lib/api/schema.ts` into a scratch directory (never overwriting the
committed files) and diff against what's checked in. A new
`scripts/check_doc_links.py` + `just docs-check-links` scan every Markdown link
in `plans/*.md` and `docs/**/*.md` and fail on a broken internal relative link
(external URLs and in-file anchors are skipped).

Verifying these gates were clean on `main` surfaced two pieces of real drift,
fixed as part of this slice: response-header key ordering in the generated
OpenAPI schema was non-deterministic (Litestar resolves a route's response
headers via `frozenset[ResponseHeader]`, so byte-diffing two regenerations
depends on Python's randomized string-hash seed) — `ui-schema`/`ui-schema-check`
now pin `PYTHONHASHSEED=0` for reproducible regeneration. Separately,
`ui/openapi.json`/`schema.ts` hadn't been regenerated after Slice A's
request-correlation-id columns landed, so the typed client was missing the new
`request_id` field; regenerated and committed (no application code touched).

### C — Browser E2E

- Add Playwright config, deterministic test app and the critical flows from the
  Plan 03 post-ship section.
- Run after UI build in CI; capture trace/screenshot only on failure.

### D — Dependency ceiling ✅ (27 July 2026)

- Pin `mlflow-skinny>=3.14,<4`.
- Add a scheduled non-blocking next-major compatibility job.

**Done:** `pyproject.toml` pins `mlflow-skinny>=3.14,<4` (`uv.lock` re-resolved
to the same `3.14.0`, only the constraint metadata changed). Added
`.github/workflows/dependency-next-major.yml` — triggers on `schedule`
(`0 5 * * 1`, Monday 05:00 UTC) plus `workflow_dispatch`, never on
`pull_request`/`push`. It force-installs `mlflow-skinny>=4,<5` via `uv pip
install --upgrade` (bypassing the pyproject ceiling for that job only,
without touching the committed lockfile) and runs the full suite against it,
with `continue-on-error: true` at both job and step level and a pass/fail
summary written to the job summary. Confirmed via the GitHub API that this
repo has no `required_status_checks` configured, so there was nothing to
accidentally wire this informational job into. Existing `.github/workflows/*`
files that run on every PR were left untouched.

## Order and verification

- A, B, C and D are independent and can run in parallel.
- Circuit breaker and provider reliability stay in Plan 05, which already owns
  the failover state and observability contract.
- Unit tests for correlation, integration tests for propagation, Playwright for
  critical UI flows, full Postgres gate after schema changes.
