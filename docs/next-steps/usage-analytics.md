# Design doc — Accurate usage analytics

> **Status:** proposed. The ledger records authoritative usage and the console
> exposes all-time per-model aggregates. Two gaps remain: routed streams do not
> attach their settled tokens to the routing decision, and there is no temporal
> usage API for charts. Execution plan:
> [`plans/10-usage-analytics.md`](../../plans/10-usage-analytics.md).

## 1. Stream attribution first

`UsageMeter.metered_stream()` already settles authoritative or estimated prompt
and completion tokens. `CompletionService._attach_routing_usage()` only receives
non-streaming responses, leaving routing decisions for streams without usage and
therefore outside the estimated-savings calculation.

Settlement should publish a small immutable result or invoke a request-scoped
callback after the ledger write. When the request was routed, that result calls
`RouterService.record_usage()` with the exact same token counts written to
`usage_event`. Failure to attach analytics must be logged and swallowed; it must
never fail settlement or the client stream.

## 2. Temporal usage contract

Add a team-scoped endpoint returning buckets rather than raw ledger rows:

```text
GET /teams/{id}/usage/timeseries
    ?from=<iso>&to=<iso>&bucket=hour|day
    &model=<name>&alias=<requested>&api_key_id=<uuid>
```

Each bucket returns calls, prompt tokens, completion tokens, cost, cache hits and
estimated-vs-authoritative counts. Filters retain the existing requested-alias
versus resolved-model semantics.

Validate bounded date ranges and bucket values at the web boundary. Authorization
is the existing `usage:read`; auditors and billing viewers see exactly the teams
they can already inspect.

## 3. Persistence and portability

Aggregation belongs behind `UsageRepository`. Keep SQLite tests and Postgres
production semantics aligned; if time bucketing needs dialect-specific SQL,
isolate it in the persistence adapter and expose one domain result type.

Index for the real query shape (`team_id`, `created_at`) already exists. Add
further indexes only after an `EXPLAIN` fixture demonstrates need. Never load the
entire ledger into Python to build chart buckets.

## 4. Console

The Usage page gains:

- cost, token and call series;
- date range and bucket selector;
- model/alias/key breakdown;
- budget limit overlay and routing/cache savings annotations where available;
- accessible table fallback and explicit partial/error states.

The current aggregate table remains for exact totals and attribution details.
Charts never compute a platform total by summing paginated page totals.

## 5. Input to later cost and policy simulation

Plan 12 owns the read-only routing simulator, including its API, console and
acceptance criteria. The temporal aggregates from this design may provide cost
baselines, but they are not sufficient to replay prompt-dependent strategies;
simulation uses an explicitly supplied evaluation corpus.
