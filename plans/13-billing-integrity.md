# Plan 13 — Billing integrity and retention

**Design doc:** [`docs/next-steps/billing-integrity.md`](../docs/next-steps/billing-integrity.md)

**Depends on:** shipped usage ledger, outbox and budgets.

**Theme:** make every priced operation billable, money exact, concurrency
cross-replica safe and history intentionally retained.

## Phase 1 — Image and cache-token pricing ✅ (27 July 2026, #388)

- Add explicit image price dimensions and Anthropic cache read/create rates.
- Extend usage/outbox entities, settlement and API aggregates.
- Reserve and settle from one normalized pricing function.
- **Done when:** image/cache-token calls bill known fixtures exactly and appear in
  budgets, usage API and console.

**Done:** the normalized pricing function lives in `domain/pricing.py` as a pure,
`Model`-free calculator so **Phase 2 can decimalize it in place** without touching
model config or the meter:

- `compute_cost(usage: BillableUsage, rates: RateCard) -> float` — the single
  usage→cost calculation. Cost is additive across independent dimensions, each
  priced at its own rate (ordinary input/output tokens, cache-write and cache-read
  tokens, and images at `image_unit_price(size, quality)`). A `None` rate prices its
  dimension at `0.0`, so a token-only call on a model with no image/cache rates is
  byte-identical to before this plan.
- `RateCard` fields: `input_cost_per_token`, `output_cost_per_token`,
  `cache_write_cost_per_token`, `cache_read_cost_per_token`, `image_cost_per_image`
  (flat fallback), `image_prices: dict[str,float]` keyed by
  `image_price_key(size, quality)` == `"{size}/{quality}"`.
- `BillableUsage` fields: `prompt_tokens`, `completion_tokens`, `cache_write_tokens`,
  `cache_read_tokens`, `image_count`, `image_size`, `image_quality` (all default to a
  no-cost zero/None).

Meter (`application/usage_meter.py`): `_rate_card(model)` projects `Model` →
`RateCard`; `_token_usage(usage_dict)` and `_image_usage(request, response)` build
`BillableUsage`. Both `admit`/`_reservation_cost` and `_settle_usage` now call
`compute_cost`. Reservation prices the prompt estimate at the priciest input-side
rate (`max(input, cache_write, cache_read)`) so it stays an upper bound once cache
tokens exist; images reserve the requested `n` and settle the count actually
returned.

New `Model`/`ModelRecord` rate fields: `cache_write_cost_per_token`,
`cache_read_cost_per_token`, `image_cost_per_image`, `image_prices` (JSON).
New ledger/outbox fields on `UsageEvent` + `usage_event`/`pending_usage_event`:
`cache_write_tokens`, `cache_read_tokens`, `image_count`. Anthropic adapter surfaces
`cache_creation_input_tokens`/`cache_read_input_tokens` as distinct dimensions
(omitted when zero, so uncached usage is unchanged). Cost aggregates (usage
timeseries, budget spend) are a plain `SUM(cost)` and needed no changes. Migration:
`c7d13f0a9b21` (rehearsed on Postgres via `just test-postgres`). Console UI form
fields for editing the new rates are deferred — cost totals already surface in the
console today; the rates are configurable via the model create/update API now.

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

**Complete (27 July 2026).** Ordinary `DELETE /teams/{id}` now branches on
whether the team has *billed history* — any `usage_event` or
`pending_usage_event` row: with none, it hard-deletes exactly as before
(regression-covered); with any, it soft-deletes (tombstones) the team instead
of touching its usage/routing/audit data. `Team.deleted_at` (a new nullable,
indexed column, migration `47e59bf43231`) is the tombstone marker; every
ordinary read path (`get`, `list`, `list_by_organization`, `list_by_ids`,
`lock_for_lifecycle`) filters it out, so a tombstoned team reads as gone to
every normal operation — the same "hidden but intact" contract the design doc
asked for. No ORM relationship in `orm.py` carries `cascade="all,
delete-orphan"` on `usage_event`/`routing_decision`/`audit_event`; the
`routing_decision` table was already found to carry no FK to `team` at all
(by design, so decision history outlives router deletion), and `audit_event`
has no FK either — the "accidental cascade" this phase guards against was
actually the *application-level* unconditional child-deletion loop in
`team_repository.py`'s `delete()`, now gated behind the billed-history check.

`Settings.team_retention_days` (default 90, `TEAM_RETENTION_DAYS` env var)
documents the anonymization-eligibility window for a tombstoned team's ledger
attribution. This phase does **not** ship the automatic anonymization job —
only the config surface and the `deleted_at` timestamp a future job needs to
compute eligibility (`deleted_at + team_retention_days`). Building that job
was judged out of scope for "choose and document," per the phase's own
wording; it's a natural, low-risk follow-up once a retention job runner
exists.

`GET /teams/{id}/export` (platform-admin only, works on a live or tombstoned
team) returns the team's full raw `usage_event` history, its full audit trail
(`AuditLog.list_by_target("team", team_id)`, a new port method), and a
routing-savings aggregate (`RoutingDecisionLog.team_savings`) rather than a
raw per-decision dump — `routing_decision` has no team-wide raw query today
(only per-router `list_decisions`), so a complete raw routing export is
deliberately deferred rather than built as a partial/misleading one.

`POST /teams/{id}/purge` is the separate, irreversible, platform-admin-only
action: it requires the team already tombstoned (409 `TeamNotSoftDeleted`
otherwise — no direct live-team purge), stages an `AuditEvent` via
`TeamService.purge_team`'s own unit of work, and only then hard-deletes the
team and its data in the SAME commit — so a crash between the two is
impossible, not just unlikely. Audit records themselves are never removed by
purge (or by ordinary delete): they are the forensic record that the
destructive action happened, and outlive it by design. Regression-tested:
non-admin purge → 403; purge of a live (non-tombstoned) team → 409; a
successful purge removes the team/usage rows (verified via a direct
repository query, not just the hidden API view) and leaves exactly one
`team.purge` audit record with the acting admin's email
(`tests/teams/test_retention_lifecycle.py`).

## Verification and sequencing

- TDD for normalized pricing and decimal rounding before migrations.
- Run full SQLite + Postgres suites for every schema phase.
- Coordinate Phase 1 with Plan 08 endpoint pricing and Phase 4 with Plan 07.
- Security review for retention/privacy and Redis reservation namespace isolation.
