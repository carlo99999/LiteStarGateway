# Plan 10 — Accurate usage analytics

**Design doc:** [`docs/next-steps/usage-analytics.md`](../docs/next-steps/usage-analytics.md)

**Depends on:** shipped usage ledger, routing decisions and console.

**Theme:** make streaming savings accurate, then add temporal cost/token/call
analytics.

## Phase 0 — Routed stream settlement

- Expose the settled token pair from `UsageMeter.metered_stream()` through a
  narrow callback/result contract.
- Attach it to the request's routing decision after the ledger write.
- Cover normal completion, provider error and client disconnect estimates.
- **Done when:** streamed and non-streamed routed calls contribute identically to
  savings, while an analytics-write failure never breaks billing or SSE.

## Phase 1 — Repository and endpoint

- Add immutable time-bucket result types and a `UsageRepository.timeseries` port.
- Implement bounded hour/day aggregation with model/alias/key filters.
- Add `/teams/{id}/usage/timeseries` under existing `usage:read` authorization.
- **Done when:** SQLite and Postgres integration tests return identical bucket
  boundaries and totals, including DST-independent UTC handling.

## Phase 2 — Console charts

- Add cost, token and call charts with date/bucket/filter controls.
- Keep an accessible tabular representation and distinguish errors from zeroes.
- Overlay budget and routing/cache savings where data exists.
- **Done when:** billing viewer, auditor and admin flows render only authorized
  team data and totals do not depend on pagination.

## Verification

- TDD per phase; Postgres tests for bucket SQL and migrations.
- Browser E2E for range/filter/chart states belongs to Plan 11's shared harness.
- Performance fixture with a realistic ledger volume; no Python full-table scan.
- Plan 12 owns the evaluation-corpus simulator; this plan only supplies the
  temporal analytics it may consume.
