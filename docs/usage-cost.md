# Design doc — Usage & cost accounting + budgets

> **Status:** Implemented. Part 2a (accounting) shipped with the `UsageEvent`
> ledger + outbox reconciler and `GET /teams/{id}/usage`. Part 2b (budgets)
> shipped as a per-team `Budget` (monthly/daily calendar window, UTC) enforced
> pre-call in `CompletionService` → `402 Payment Required`, managed via
> `GET/PUT/DELETE /teams/{id}/budget` (set/remove is platform-admin only).
> Open item from §4: the enforcement read is an indexed SUM per request, not
> yet a running counter; per-key budgets and block-vs-alert are not implemented.
>
> **Burst overshoot bound:** the gate also counts an in-memory, per-process
> reservation for every admitted-but-unsettled request (estimated prompt +
> requested `max_tokens` output ceiling × `n` choices, released at settlement),
> so a burst of concurrent requests — streams especially — can't all pass under
> a nearly-exhausted limit. The reservation is computed from the *sanitized*
> request, so client-supplied `max_tokens`/`n` are already clamped and can't
> poison the gate. The residual overshoot is: requests already in flight
> when the limit is crossed still complete; requests without a max-tokens field
> reserve only their prompt estimate; and the reservation is per replica, so the
> bound scales with replica count.
>
> **Outbox spend & quarantine:** the gate also counts dead-lettered spend still
> in the outbox (`pending_usage_event`) so a ledger-write degradation isn't a
> budget-bypass window. Rows quarantined after `MAX_RECONCILE_ATTEMPTS` failed
> reconciles are **excluded** from the gate: they will never drain into the
> ledger (so they never actually bill), and counting them would permanently
> shrink the team's usable budget by a phantom amount for the rest of the
> window (M28).

## 1. Goal

Track token usage and cost per team/model and, when present, API key; expose it,
and **enforce budgets** (hard spend caps). Session-authenticated Playground calls
use a null `api_key_id` and remain attributable to their human actor in the audit
log. The `Model` already carries `input_cost_per_token` /
`output_cost_per_token`, but nothing aggregates them today. You can't safely run a
multi-tenant gateway without accounting + limits.

## 2. Two parts

### 2a. Accounting (record every call's usage/cost)

- After each call, read `usage` from the provider response (prompt/completion
  tokens), compute cost = tokens × the `Model` cost fields, and persist a
  **`UsageEvent`** (team_id, model_id, optional key_id, op, tokens, cost, ts, status).
- This overlaps with observability (`adding-observability-via-mlflow`): MLflow is
  for traces/analytics; the `UsageEvent` table is the **authoritative billing
  record** and the source for budget enforcement. Keep both (different purposes),
  written from the same post-call hook.
- Expose `GET /usage` (team-scoped; platform-admin can see all) with filters +
  aggregation (by day/model/key).

Each aggregate preserves both sides of callable resolution: `requested_alias`
(what the client sent) and `resolved_model_id`/`canonical_model_name` (what was
actually billed), plus callable origin and source-team provenance. Historical
rows created before the identity migration report alias/origin as `unknown`;
the service never guesses them.

`GET /teams/{id}/usage` filter semantics are explicit:

- `model` matches either requested alias or canonical model name (compatible,
  broad search);
- `alias` matches only the exact requested alias;
- `resolved_model_id` matches only the stable billed model identity;
- `api_key_id` retains its existing exact caller filter.

> **Known gap — image generation is unbilled (L22).** The cost model is
> per-token (`input_cost_per_token`/`output_cost_per_token`), but image responses
> (DALL·E, Imagen) carry no token usage, so an image call records a `UsageEvent`
> with 0 cost and reserves ~0 against the budget. Image spend is therefore
> invisible to budgets and aggregates. This is a missing pricing dimension, not a
> logic bug; when image traffic matters, add a per-image price to `Model` (e.g.
> `image_cost_per_call`, optionally by size/quality) and bill it in settlement.
> Until then, treat image usage as out of budget scope.
> Plan 13 turns this into an implementation slice and also gives Anthropic cache
> creation/read tokens separate rates instead of folding them into normal input.

### 2b. Budgets / quotas (enforce)

- A **`Budget`** per team (and optionally per key): limit + window (monthly/daily)
  - action (block vs alert).
- Enforcement is **pre-call**: check accumulated spend for the window before
  dispatching; over-limit → `402 Payment Required` / `429`. Keep a fast running
  counter (cache/store) so enforcement isn't a heavy aggregate query per request.

## 3. Placement

```text
domain/entities.py      UsageEvent, Budget
domain/ports.py         UsageRepository, BudgetRepository
application/completion_service.py
    pre-call: budget check (fail fast if over)
    post-call: settle and record UsageEvent before completing the response
infrastructure/web       GET /usage, budget CRUD (admin/team-admin)
```

The primary ledger write is **on the response path** so usage remains immediately
consistent. A durable outbox recovers transient write failures in the background;
trace export is the separate off-path sink. Budget checks are also on the path
(they must gate the call) but remain cheap through a running counter rather than
a full SUM per request.

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
6. **Money representation**: migrate authoritative rates, costs, budgets and
   savings from binary `float` to fixed-precision `Decimal`/SQL `NUMERIC`, with
   one documented rounding rule.
7. **Cross-replica admission**: replace process-local in-flight spend with an
   atomic reservation port (Redis in multi-replica production, in-memory in dev)
   while keeping Postgres settlement authoritative.
8. **Time series**: the current endpoint is an all-time grouped view. Plan 10 adds
   bounded hour/day buckets and model/alias/key filters for charts.
9. **Retention**: deleting a team currently removes its ledger. Decide explicit
   soft-delete/anonymization/export/purge semantics rather than relying on FK
   cascades; see the Round 12 deferred product decision and Plan 13.

## 5. Testing

- Pure cost calc (tokens × rates, per op).
- Budget gate: under/over/at-limit; window reset; block vs alert.
- Streaming/missing-usage → estimated cost path.
- Recording failure must not break the request (fail-safe, like the sink).

## 6. Rollout

1. `feat/usage-accounting` — `UsageEvent` + post-call recording + `GET /usage`.
2. `feat/budgets` — `Budget` + pre-call enforcement + running counter.
