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
