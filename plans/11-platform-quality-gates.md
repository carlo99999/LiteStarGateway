# Plan 11 — Platform quality gates

**Design sources:** [`docs/logging.md`](../docs/logging.md) for correlation IDs,
[`docs/db-migrations.md`](../docs/db-migrations.md) for migration drift, and
[`plans/03-admin-ui.md`](03-admin-ui.md) for OpenAPI/browser quality.

**Depends on:** shipped logging, CI and admin console.

**Theme:** make requests traceable and release drift/user-flow regressions
impossible to merge unnoticed.

## Independent slices

### A — Request correlation

- Request-ID middleware/hook, trusted-proxy validation and response header.
- Bind to structlog and propagate to audit/usage/routing/trace records.
- Tests prove ID consistency and absence of secrets.

### B — CI drift gates

- Wire `just migration-check`.
- Regenerate OpenAPI + TypeScript schema into temporary outputs and diff.
- Add a Markdown link checker for roadmap/design docs.

### C — Browser E2E

- Add Playwright config, deterministic test app and the critical flows from the
  Plan 03 post-ship section.
- Run after UI build in CI; capture trace/screenshot only on failure.

### D — Dependency ceiling

- Pin `mlflow-skinny>=3.14,<4`.
- Add a scheduled non-blocking next-major compatibility job.

## Order and verification

- A, B, C and D are independent and can run in parallel.
- Circuit breaker and provider reliability stay in Plan 05, which already owns
  the failover state and observability contract.
- Unit tests for correlation, integration tests for propagation, Playwright for
  critical UI flows, full Postgres gate after schema changes.
