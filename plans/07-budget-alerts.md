# Plan 07 — Proactive budget alerts

**Design doc:** [docs/next-steps/budget-alerts.md](../docs/next-steps/budget-alerts.md)
**Depends on:** usage accounting + budgets (shipped — `application/usage_meter.py`,
`domain/entities/billing.py`, `docs/usage-cost.md`); the durable usage outbox
(`domain/ports/usage.py:47`, `infrastructure/usage_reconciler.py`); the SSRF-guarded
webhook egress (`application/routing/webhook.py:59`). Console work rides on
Plan 03 (Admin UI).
**Theme:** notify a team (webhook/email) as spend crosses configurable thresholds
(50 % / 80 % / 100 % of the daily or monthly cap) **before** the hard `402`,
evaluated at settlement and delivered off the hot path through a durable outbox.

## Scope

Additive to enforcement — the pre-call gate (`usage_meter.py:239` `admit` → `402`)
is untouched. Each threshold fires **at most once per budget period**, keyed on
`(team_id, window, period_start, threshold)` and persisted so it survives restarts.

## Phases

### Phase 0 — Threshold model + fired-threshold persistence + dedup logic

- Domain: extend the budget config with a per-team `thresholds: list[int]`
  (percent of `Budget.limit_cost`, `billing.py:16`); add a `BudgetAlertState`
  entity + `BudgetAlertStateRepository` port (`domain/ports/`) for the dedup
  ledger keyed on `(team_id, window, period_start, threshold)`.
- Pure evaluation helper: given committed spend, cap, thresholds, and the set of
  already-fired keys for the current `period_start` (`window_start`,
  `domain/budget.py`), return the thresholds newly crossed. No I/O.
- Alembic migration for `budget_alert_state` with a unique constraint on the key.
- **Done when:** unit tests prove the helper returns each crossed-but-unfired
  threshold once, returns nothing when all crossed thresholds are already fired,
  and that a new `period_start` yields a fresh (empty) fired set. Boundary
  validation (1..100, sorted, deduped) tested.

### Phase 1 — Evaluate at settlement + enqueue to the outbox

- Hook the evaluation into `UsageMeter.settle_ok` (`usage_meter.py:313`), right
  after the ledger write, reusing `spend_since` (`domain/ports/usage.py:42`) for
  window total. Streams inherit it via `_finalize_stream_billing`
  (`usage_meter.py:659` → `settle_ok` `:736`) — no separate wiring.
- For each newly-crossed threshold, in the **same transaction**: insert the dedup
  key and enqueue a `pending_budget_alert` outbox row (new `enqueue_alert`/
  `reconcile_alerts` port methods mirroring `usage.py:47-55`).
- **Done when:** integration tests prove crossing 80 % enqueues exactly one alert
  and re-crossing (a second settlement still ≥ 80 %) enqueues none; dedup key +
  outbox row commit atomically; rollover to a new period re-arms all thresholds.

### Phase 2 — Webhook channel via SSRF-guarded egress

- `NotificationChannel` port (`domain/ports/`) with a webhook adapter in
  `infrastructure/` that **reuses the routing webhook egress guard verbatim**:
  literal + resolved-IP `_is_blocked` deny-list (`webhook.py:59`), per-call
  DNS-rebinding re-check, IP-pinned POST retaining Host/SNI (`webhook.py:121`).
- Background outbox worker (a lifespan like `usage_reconciler.py:57`) drains
  `pending_budget_alert`, dispatches via configured channels, deletes on success,
  retries on failure.
- **Done when:** an enqueued alert delivers to a public webhook; a private/
  loopback/link-local URL is rejected at config time and at send; a failing
  webhook is retried and never surfaces to the request.

### Phase 3 — Email channel + console config

- Second `NotificationChannel` adapter in `infrastructure/` (SMTP or provider
  API); port stays abstract, no infra types cross into `domain`/`application`.
- Config surface on the existing budget endpoints (`GET/PUT/DELETE
  /teams/{id}/budget`): thresholds + channel settings, boundary-validated.
- Console: **Alerts** section under **Budgets** (`plans/03-admin-ui.md`) — set
  thresholds, configure channels, list recent fired alerts. Read-only first,
  mutations with the budget mutation slice.
- **Done when:** an email alert delivers via a fake SMTP/provider in tests;
  thresholds + channels round-trip through the API; the console renders and
  edits them RBAC-aware.

## TDD strategy

- **Unit:** the Phase 0 helper — each threshold fires exactly once per period;
  the fired set resets on `period_start` change (rollover); threshold list
  validation. Table-driven over spend/cap/threshold combinations.
- **Integration:** through the completion path — one settlement crossing 80 %
  enqueues one alert; a subsequent settlement still ≥ 80 % enqueues none; the
  100 % alert fires alongside (not instead of) the existing `402` from `admit`.
- **Regression (§7 of the design):** a channel that raises (broken webhook, SMTP
  down) must **not** affect the inference request — assert the completion
  succeeds and the failure stays in the outbox for retry, mirroring the
  billing-sink fail-safe test (`usage_meter.py:355`, `:421`).

## Risks & mitigations

- **Duplicate alerts** → persisted dedup key `(team, window, period_start,
  threshold)` with a DB unique constraint; the insert + outbox enqueue share one
  transaction, so a fired threshold can neither double-send nor be lost.
- **Spend races across replicas** → evaluation reads the shared `spend_since`
  aggregate and the dedup insert's unique constraint makes concurrent
  settlements safe (loser hits the conflict and skips — same PK-conflict strategy
  as `usage_reconciler.py:8`). No `DistributedLock` (`domain/ports/lock.py:10`)
  needed.
- **Delivery failures** → durable outbox with a retrying background worker (not
  fire-and-forget); rows delete only on success.
- **SSRF via operator-supplied webhook URL** → reuse the routing webhook guard
  unchanged (`webhook.py:59-140`); reject non-public targets at config time and
  re-validate on every send to resist DNS rebinding.
- **Alert lag vs enforcement** → alerts trail enforcement by one settlement (they
  need actual, not reserved, spend); documented and accepted (§3, §6 of design).

## Execution

- **One branch per phase**, TDD (RED→GREEN), gated with `just test` / `just lint`
  / `just typecheck` / `just pre-commit` per `plans/README.md`.
- Phases 0→1→2→3 are strictly sequential (each builds on the prior); Phase 3's
  console slice can split from its backend slice once the API exists, and can run
  in the Plan 03 UI track.
- Phase 1 touches `usage_meter.py` — coordinate with any concurrent billing work
  to share the Alembic migration head (per the parallel-worktree convention).
- Run `just test-postgres` for the migration-affecting phases (0 and 1) before
  relying on CI.
