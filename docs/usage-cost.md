# Design doc — Usage & cost accounting + budgets

> **Status:** Draft / parked (pre-v1). Branch `adding-usage-cost`. No code yet.

## 1. Goal

Track token usage and cost per team/model/key, expose it, and **enforce budgets**
(hard spend caps). The `Model` already carries `input_cost_per_token` /
`output_cost_per_token`, but nothing aggregates them today. You can't safely run a
multi-tenant gateway without accounting + limits.

## 2. Two parts

### 2a. Accounting (record every call's usage/cost)
- After each call, read `usage` from the provider response (prompt/completion
  tokens), compute cost = tokens × the `Model` cost fields, and persist a
  **`UsageEvent`** (team_id, model_id, key_id, op, tokens, cost, ts, status).
- This overlaps with observability (`adding-observability-via-mlflow`): MLflow is
  for traces/analytics; the `UsageEvent` table is the **authoritative billing
  record** and the source for budget enforcement. Keep both (different purposes),
  written from the same post-call hook.
- Expose `GET /usage` (team-scoped; platform-admin can see all) with filters +
  aggregation (by day/model/key).

### 2b. Budgets / quotas (enforce)
- A **`Budget`** per team (and optionally per key): limit + window (monthly/daily)
  + action (block vs alert).
- Enforcement is **pre-call**: check accumulated spend for the window before
  dispatching; over-limit → `402 Payment Required` / `429`. Keep a fast running
  counter (cache/store) so enforcement isn't a heavy aggregate query per request.

## 3. Placement

```
domain/entities.py      UsageEvent, Budget
domain/ports.py         UsageRepository, BudgetRepository
application/completion_service.py
    pre-call: budget check (fail fast if over)
    post-call: record UsageEvent (off hot path, like the trace sink)
infrastructure/web       GET /usage, budget CRUD (admin/team-admin)
```

Recording goes **off the hot path** (background task), like the observability
sink; budget checks are **on** the path (they must gate the call) but must be
cheap (running counter, not a full SUM per request).

## 4. Open decisions

1. **Cost source of truth**: static per-`Model` rates (current) vs a provider
   price table. Start with the `Model` fields.
2. **Estimation vs actuals**: some providers don't return token usage for every
   op (esp. streaming/images) — need a token estimator fallback (tokenizer) or
   mark cost as estimated.
3. **Budget window reset** (calendar month vs rolling) and **per-key vs per-team**.
4. **Enforcement counter store**: DB vs Redis; must be atomic under concurrency.
5. **Overlap with MLflow**: confirm the split — `UsageEvent` = billing truth,
   MLflow = analytics/traces.

## 5. Testing

- Pure cost calc (tokens × rates, per op).
- Budget gate: under/over/at-limit; window reset; block vs alert.
- Streaming/missing-usage → estimated cost path.
- Recording failure must not break the request (fail-safe, like the sink).

## 6. Rollout

1. `feat/usage-accounting` — `UsageEvent` + post-call recording + `GET /usage`.
2. `feat/budgets` — `Budget` + pre-call enforcement + running counter.
