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

**Complete (27 July 2026, #378).** `UsageMeter.metered_stream()` gained an
optional `on_settled: Callable[[int, int], Awaitable[None]] | None` callback,
threaded through to `_finalize_stream_billing` and invoked with the exact
`(prompt_tokens, completion_tokens)` pair once they are actually billed —
never a return value, since settlement happens well after `open_chat_stream`
has already handed the generator back to the SSE layer. `settle_ok` was
changed to return its settled `(prompt, completion, cost)` instead of `None`,
so the normal-completion/disconnect branch has the exact counts without
re-deriving them from the response body.

The three termination cases:

- **Normal completion** — real settled counts from `settle_ok`.
- **Provider error mid-stream** (some output produced) — the same
  partial/estimated counts the existing M26 billing path already bills;
  the callback fires right after that branch's `_bill` call.
- **Client disconnect** — not an `Exception` in `metered_stream`'s `except`
  clause, so it falls into the same `settle_ok` branch as normal completion
  and gets the same estimate-then-bill-then-notify treatment.
- **Zero-consumption provider rejection** (`produced_nothing`, M26) — nothing
  is billed, so the callback never fires, matching the non-streaming
  behavior for a generic (non-`UpstreamResponseInvalid`) exception, which
  also never attaches usage.

`CompletionService._metered` (shared by `open_chat_stream`, its failover retry
loop, and `open_responses_stream`) wires `on_settled=self._record_router_usage`
— a small helper extracted from the pre-existing non-streaming
`_attach_routing_usage`, so both surfaces funnel into the same single
`RouterService.record_usage()` call.

The fail-safe guard is two independent nets around the same secondary write:
`UsageMeter._notify_settled` swallows and logs any exception the callback
raises, on top of `RouterService.record_usage`'s own pre-existing
swallow-and-log. Neither the billing write (already committed by the time the
callback runs) nor the in-flight SSE response can be broken by a bug in this
bookkeeping. `metered_native_stream`/`metered_gemini_stream` (native
Anthropic/Gemini passthrough) were deliberately left unwired — those surfaces
don't go through `RouterService` routing today, so there is no decision to
attach usage to. No migration was needed; this only wires existing settlement
data through the existing `RoutingDecisionLog.update_usage()` path. Covered by
`tests/completions/test_stream_routing_usage.py` (normal completion, mid-stream
error, client disconnect, and a raising `record_usage` that must not break the
stream or billing).

## Phase 1 — Repository and endpoint

- Add immutable time-bucket result types and a `UsageRepository.timeseries` port.
- Implement bounded hour/day aggregation with model/alias/key filters.
- Add `/teams/{id}/usage/timeseries` under existing `usage:read` authorization.
- **Done when:** SQLite and Postgres integration tests return identical bucket
  boundaries and totals, including DST-independent UTC handling.

**Complete (27 July 2026, #379).** Two frozen dataclasses in
`domain/entities/billing.py`: `UsageBucket` (`bucket_start` — UTC, tz-aware —
plus `request_count`/`prompt_tokens`/`completion_tokens`/`cost`), and
`UsageTimeseries` (`team_id`/`granularity`/`start`/`end`/`buckets`) as the
metadata-carrying container the controller assembles the response from.
`UsageRepository.timeseries(team_id, *, start, end, granularity, model_name=,
requested_alias=, api_key_id=)` was added to the existing port (no parallel
port), mirroring `aggregate`'s filter semantics exactly (`model_name` = broad
alias-or-canonical match; `requested_alias`/`api_key_id` exact); a bucket is
only emitted when it has ≥1 matching event, never zero-filled.

`SQLAlchemyUsageRepository.timeseries` bucketing happens entirely in SQL — one
`GROUP BY` over a dialect-portable bucket-key expression, never a Python-side
scan:

- **Postgres:** `date_trunc(granularity, created_at AT TIME ZONE 'UTC')`. The
  explicit `AT TIME ZONE 'UTC'` conversion matters: `timestamptz` truncation
  otherwise happens in the *session* timezone, which would make bucket
  boundaries depend on whatever timezone the connection happens to be in.
- **SQLite:** `strftime('%Y-%m-%d %H:00:00', created_at)` (hour) /
  `strftime('%Y-%m-%d 00:00:00', created_at)` (day) — `created_at` is already
  stored as a plain UTC string by `DateTimeUTC`, so no conversion is needed,
  only truncation via a fixed minutes/seconds suffix.

Both branches represent a UTC wall-clock instant with no reference to server
or session-local time, so a range spanning a real DST transition still
produces evenly-spaced, non-shifted buckets — covered by
`tests/teams/test_usage_timeseries.py::test_timeseries_is_dst_independent_across_a_us_dst_transition`,
which seeds ten consecutive UTC hours across 2026-03-08 (a US spring-forward
date) and asserts every consecutive pair of `bucket_start`s is exactly one
hour apart. The same test file runs its repository-level cases against both
SQLite and Postgres via the shared `database_url` fixture (`just
test-postgres`), asserting identical bucket boundaries/totals/filter behavior
on both dialects.

`GET /teams/{team_id}/usage/timeseries` (`infrastructure/web/teams/controller.py`)
sits next to the existing `usage` aggregate endpoint, under the same
`usage:read` RBAC scope and the same `ensure_principal_team_permission`
check (JWT or management-scoped API key, own team only — verified by
`tests/rbac/test_extended_team_roles.py::test_billing_viewer_cannot_read_another_teams_usage_timeseries`).
Query params: `start`, `end` (RFC3339 datetimes), `granularity` (`hour` |
`day`), and the same `model`/`alias`/`api_key_id` filters as `usage`. A new
`InvalidUsageQuery` domain error (→ 400) rejects an unknown granularity or
`end <= start`. The response (`UsageTimeseriesResponse`) is never paginated —
a bounded date range already bounds the row count by construction, so totals
can't depend on pagination, satisfying that "Done when" criterion ahead of
Phase 2.

Deferred to Phase 2 (as scoped): dense/gap-filled buckets, cache-hit and
estimated-vs-authoritative breakdowns per bucket (mentioned in the design
doc's contract sketch but not required by this phase's "Done when"), and all
console rendering.

## Phase 2 — Console charts

- Add cost, token and call charts with date/bucket/filter controls.
- Keep an accessible tabular representation and distinguish errors from zeroes.
- Overlay budget and routing/cache savings where data exists.
- **Done when:** billing viewer, auditor and admin flows render only authorized
  team data and totals do not depend on pagination.

**Complete (27 July 2026, #380).** This completes Plan 10 entirely — all
three phases (streamed-usage attribution, the timeseries endpoint, and the
console charts below) are now shipped.

Phase 1's endpoint returned one scalar series per call — filtered, but not
grouped — so a per-team "which models, how often" chart would otherwise need
one request per model. Rather than have the console fan out N requests,
`UsageRepository.timeseries` (port + `SQLAlchemyUsageRepository`) gained an
optional `group_by: Literal["model"] | None` parameter: when set, the
existing single `GROUP BY bucket_key` SQL aggregate becomes
`GROUP BY bucket_key, group_key` (still one query, still dialect-portable),
emitting one row per `(bucket_start, group_key)` pair instead of one per
`bucket_start`. `group_key` is `coalesce(requested_alias,
canonical_model_name)` — the same label `UsageResponse.from_aggregate`
already uses for the per-model table. `UsageBucket` gained a matching
optional `group_key: str | None` field, and `GET
/teams/{team_id}/usage/timeseries` gained the `group_by=model` query param
(400 on any other value), covered by
`tests/teams/test_usage_timeseries.py` (repository-level grouping, the
alias-vs-canonical-name fallback, the ungrouped `group_key is None` case, the
400 validation, and an end-to-end HTTP test through a real
`/v1/chat/completions` call).

The console (`ui/src/features/usage/`) gained `UsageChartsPanel.tsx`, mounted
below the existing per-model usage table in `UsagePage.tsx` (additive only,
same `usage:read`-gated `teamId` the existing table already uses — no new
RBAC surface). It reuses Phase 1's existing `start`/`end`/`granularity`/
`alias`/`api_key_id` filters, adds a cost/calls/tokens metric toggle, and
renders a dependency-free inline-SVG stacked-area chart
(`StackedAreaChart.tsx` — no charting library was added; none was already a
dependency and `ui/package.json` still has none) with a legend, hover
crosshair/tooltip, and a fixed, never-cycled categorical color order (folds
to "other" past 8 series) drawn from the validated dataviz-skill palette,
wired as `--chart-1..8` CSS custom properties for both themes in
`globals.css`. The pure grouping/aggregation logic
(`buildStackedSeries`/`totalMetric`/`maxStackedValue` in
`timeseriesChart.ts`) is unit-tested with plain `node:test`
(`timeseriesChart.test.ts`), mirroring `features/budgets/alertConfig.test.ts`'s
convention — the chart's visual rendering itself was exercised by running the
Vite dev server against a live gateway rather than a rendering test, matching
this repo's established pattern.

Three explicit states satisfy "distinguish errors from zeroes": an invalid
date range, a request/query error (`EmptyState` with the error message,
`border-destructive/40`), and a genuinely empty range (`no usage in range` —
visually and textually distinct from the error state) are never conflated.
An accessible table (bucket/model/calls/tokens/cost rows) always renders
alongside the chart, built from the same non-paginated bucket list the chart
uses, so neither view's total can depend on pagination.

The budget/cache-savings overlay is opportunistic, per the plan's wording:
`OverlayStats` reuses the *existing* `getTeamBudget` and `teamCacheSavings`
endpoints only (no new backend aggregation), silently omitting itself on
error/absence exactly like `ModelsPage.tsx`'s pre-existing
`CacheSavingsPanel`. Per-router routing savings was deliberately **not**
overlaid: `routerSavings` is scoped to one specific router with no team-wide
aggregate to draw from, so overlaying it here would mean either picking an
arbitrary router or adding new aggregation — neither of which this phase
asks for.

## Verification

- TDD per phase; Postgres tests for bucket SQL and migrations.
- Browser E2E for range/filter/chart states belongs to Plan 11's shared harness.
- Performance fixture with a realistic ledger volume; no Python full-table scan.
- Plan 12 owns the evaluation-corpus simulator; this plan only supplies the
  temporal analytics it may consume.
