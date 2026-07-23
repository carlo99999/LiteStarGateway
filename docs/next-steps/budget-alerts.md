# Design doc — Proactive budget alerts

> **Status:** proposed. Complements the existing hard budget enforcement
> (`domain/entities/billing.py:12` `Budget`, pre-call gate in
> `application/usage_meter.py:239` `admit`). Today a team either has budget or
> gets a `402` at the gate — there is no signal as spend *approaches* the cap.
> This doc adds **proactive threshold notifications** (e.g. 50 % / 80 % / 100 %
> of the daily or monthly cap) delivered off the hot path, before the hard block.

## 1. Goal

Give operators a heads-up while there is still time to act. A team crossing
80 % of its monthly cap should trigger a webhook or email *now*, not discover the
limit only when a request 402s. Enforcement is unchanged; alerts are additive.

## 2. The alert model

A budget gains an optional, per-team **threshold set** expressed as percentages
of the cap — e.g. `[50, 80, 100]`. Each percentage, applied to `Budget.limit_cost`
(`billing.py:16`), is an absolute USD trigger point for the current window.

- Thresholds are per-team and per-budget (daily and monthly caps each carry
  their own set, since `Budget.window` is one of `MONTHLY`/`DAILY`,
  `domain/entities/enums.py:42`).
- **Core invariant — each threshold fires AT MOST ONCE per budget period.**
  Crossing 80 % emits exactly one alert for that window; every subsequent
  settlement that is still ≥ 80 % (and < the next threshold) emits nothing.
  Dedup is keyed by **`(team_id, window, period_start, threshold)`** and
  **persisted**, so it survives process restarts and is not re-evaluated as
  fresh on every request. `period_start` is `window_start(budget.window, now)`
  (`domain/budget.py`), the same calendar anchor the gate already uses at
  `usage_meter.py:262`.

The persisted record is a new `budget_alert_state` row (one per fired
`(team, window, period_start, threshold)`), added via an Alembic migration per
`CONTRIBUTING.md`. It is a small dedup ledger, not a metric store — spend itself
stays in `usage_event`.

## 3. Where the crossing is evaluated — at settlement

The natural evaluation point is **settlement, when actual cost is known**, not
pre-call. Spend becomes authoritative in `UsageMeter.settle_ok`
(`application/usage_meter.py:313`), which computes `cost` via `_parse_usage` and
writes the ledger through `_bill` → `_record_usage` → `usage.record`
(`usage_meter.py:342`, `:454`, `:419`). Streams settle through the same path in
`_finalize_stream_billing` (`usage_meter.py:659`, which calls `settle_ok` at
`:736`). The threshold check hooks in **right after the ledger write succeeds**,
inside `settle_ok`, so it sees committed dollars.

**Why not pre-call.** `admit` (`usage_meter.py:239`) works with a *reservation*
(pessimistic estimate: prompt + output ceiling, `_reservation_cost`
`usage_meter.py:166`), not actual spend — evaluating there would fire alerts on
money that may never be billed (a stream that disconnects early, an over-clamped
`max_tokens`). Actual spend is only known at settlement. Alerts therefore trail
enforcement by one request, which is the correct trade: enforcement must be
conservative and pre-call; notification must be accurate and post-fact.

The evaluation itself is cheap: `settle_ok` already knows `team_id` and the new
cost; total window spend is the existing indexed aggregate `spend_since`
(`domain/ports/usage.py:42`). Compare `spend_since` against each threshold's USD
value; for every threshold now crossed whose dedup key is not yet persisted,
enqueue one alert and record the key in the same transaction.

## 4. Delivery — through the durable outbox

Alerts are **not** delivered inline from `settle_ok`. They are enqueued as
events and delivered by a background worker, mirroring the usage-billing outbox
(`enqueue_pending`/`reconcile_pending`, `domain/ports/usage.py:47`, drained by
`infrastructure/usage_reconciler.py:37`). This gives us the same guarantees the
billing pipeline already relies on: delivery is decoupled from the request,
retried on failure, and survives restarts (not fire-and-forget).

A new `pending_budget_alert` outbox row is written in the *same transaction* as
the dedup-key insert of §3, so "we marked the threshold fired" and "we have an
alert to deliver" commit atomically — no fired-but-never-sent gap, no
sent-but-not-deduped duplicate. A periodic worker (a lifespan like the usage
reconciler, `usage_reconciler.py:57`) drains the outbox and dispatches each alert
through the configured channels, deleting the row only on success.

### Channels — a pluggable port

Delivery targets sit behind a domain port, abstract in `domain/ports/`:

```python
class NotificationChannel(Protocol):
    async def send(self, alert: BudgetAlert) -> None: ...
```

- **Webhook** — reuse the SSRF-guarded egress pattern from the routing webhook
  strategy verbatim: literal-IP + resolved-address deny-list `_is_blocked`
  (`application/routing/webhook.py:59`), DNS-rebinding re-check on every call, and
  IP-pinned POST that keeps Host/SNI (`webhook.py:121`). A budget-alert webhook
  targeting a private/loopback/link-local address is rejected the same way. This
  guard is non-negotiable: the URL is operator-supplied and points outbound.
- **Email** — a second adapter in `infrastructure/` (SMTP or a provider API).
  The **port stays abstract**; SMTP/credentials/provider choice are an
  infrastructure concern and never leak into `domain`/`application` (hexagonal
  boundary, `CONTRIBUTING.md`).

The outbox worker resolves configured channels per team and calls `send`;
a channel raising propagates as an outbox failure → retry, never touching the
request.

## 5. Period rollover

When the window rolls over (calendar day/month, `window_start` `domain/budget.py`),
`period_start` changes, so the dedup key changes and the fired-threshold set is
effectively cleared for the new period — 50 %/80 %/100 % can each fire once
again. No explicit reset job is needed: keying on `period_start` makes rollover
implicit. A retention sweep may prune `budget_alert_state` rows older than the
current period, but correctness does not depend on it.

## 6. The 100 % threshold vs the hard 402

The 100 % alert **complements, does not replace** enforcement. The pre-call gate
in `admit` (`usage_meter.py:268`) still raises `BudgetExceeded` → `402` exactly
as today; that behavior is untouched. The 100 % alert simply notifies that the
cap has been reached — operators get told the same instant blocking begins,
rather than inferring it from a spike of 402s. Because alerts trail by one
settlement, the 100 % alert may land just after the first 402; that ordering is
acceptable and documented.

## 7. Failure policy

**A failed alert delivery must NEVER affect the inference request.** This is
guaranteed structurally: evaluation only enqueues an outbox row (a local insert
in the settlement transaction), and *all* delivery happens in the background
worker, entirely off the hot path — the same fail-safe stance the billing sink
and trace emitter already take (`usage_meter.py:355`, `:421`). A broken webhook,
an SMTP outage, or a slow provider degrades to retries and logs; the user's
completion is unaffected. If the settlement transaction itself fails, the ledger
write fails too and the existing outbox/at-most-once behavior of `_record_usage`
(`usage_meter.py:407`) governs — alerts never widen that failure surface.

## 8. Config surface & console

- **Config:** thresholds and channel config attach to the team's budget —
  per-team `thresholds: list[int]` plus channel settings (webhook URL + optional
  bearer, email recipients). Managed alongside the existing budget endpoints
  (`GET/PUT/DELETE /teams/{id}/budget`, per `docs/usage-cost.md`). Validate
  thresholds at the boundary: integers in `1..100`, sorted, de-duplicated.
- **Console:** an **Alerts** section under **Budgets** in the admin UI
  (`plans/03-admin-ui.md`) — set thresholds, configure channels, and view recent
  fired alerts (read from `budget_alert_state`). Read-only first, mutations with
  the rest of the budget mutation slice.

## 9. Cross-replica coordination

`spend_since` is a shared DB aggregate, so all replicas see the same committed
spend; the dedup insert's unique constraint on `(team, window, period_start,
threshold)` makes concurrent settlements on different replicas safe — the loser
hits the conflict and skips, exactly like the usage reconciler's PK-conflict
strategy (`usage_reconciler.py:8`). No `DistributedLock` (`domain/ports/lock.py:10`)
is required for evaluation. `REDIS_URL` (`config.py:170`) remains optional and is
not a dependency of this feature.

## 10. Explicit non-goals (v1)

- **No alerting DSL** — thresholds are a flat percentage list, nothing
  composable/conditional.
- **No per-user or per-API-key alerts** — team + window only (mirrors how
  budgets themselves are scoped today).
- **No alert history analytics / dashboards** beyond the recent-fired list.
- **No new delivery channels** beyond webhook + email in v1 (the port makes more
  additive later).
- Enforcement semantics are **out of scope** — this doc only adds notification.
