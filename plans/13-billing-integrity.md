# Plan 13 — Billing integrity and retention

**Design doc:** [`docs/next-steps/billing-integrity.md`](../docs/next-steps/billing-integrity.md)

**Depends on:** shipped usage ledger, outbox and budgets.

**Theme:** make every priced operation billable, money exact, concurrency
cross-replica safe and history intentionally retained.

## Phase 1 — Image and cache-token pricing

- Add explicit image price dimensions and Anthropic cache read/create rates.
- Extend usage/outbox entities, settlement and API aggregates.
- Reserve and settle from one normalized pricing function.
- **Done when:** image/cache-token calls bill known fixtures exactly and appear in
  budgets, usage API and console.

## Phase 2 — Decimal migration

- Define domain precision/rounding and replace authoritative money floats.
- Add a Postgres-rehearsed Alembic migration and compatibility serializer.
- Update cost/savings tests to exact decimal assertions.
- **Done when:** repeated aggregation is order-independent and no binary-float
  drift reaches budget comparisons.

## Phase 3 — Distributed reservations

- Add `BudgetReservationStore` with atomic reserve/release/TTL keyed by an
  internal, server-generated reservation UUID. It is distinct from the
  correlation ID, which may accept a validated client value and is not a
  uniqueness boundary.
- Implement in-memory and Redis adapters.
- Test multi-replica contention, process-death expiry and idempotent release.

## Phase 4 — Per-key budgets and modes

- Add key-scoped limit/window plus `block|alert`.
- Enforce team and key policies in one admission transaction.
- Integrate Plan 07 thresholds without duplicate spend calculations.

## Phase 5 — Retention lifecycle

- Choose and document retention/anonymization periods.
- Soft-delete teams with billed history; add explicit export and audited purge.
- Prevent accidental FK cascades across usage, decisions and audit data.

## Verification and sequencing

- TDD for normalized pricing and decimal rounding before migrations.
- Run full SQLite + Postgres suites for every schema phase.
- Coordinate Phase 1 with Plan 08 endpoint pricing and Phase 4 with Plan 07.
- Security review for retention/privacy and Redis reservation namespace isolation.
