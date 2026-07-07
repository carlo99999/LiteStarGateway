# Round 3 — Money & concurrency review (2026-07-03)

[← Index](INDEX.md)

Third whole-codebase pass, focused on the ~28 commits merged since Round 2: team budgets +
pre-call enforcement, login lockout, usage outbox + reconciler, MLflow ops metrics, upstream
error classification, distributed locks, unit-of-work refactor, and service principals with
scoped API keys. Run by four parallel reviewers (security, Python/async, architecture,
billing/concurrency); every finding below was **re-verified against source** before inclusion.
Baseline: **303 tests pass**, `ruff` and `pyrefly` clean, no tracked secrets.

The dedicated security pass found **no new security issues** — budget-gate authorization,
service-principal scope enforcement (two-layer `enabled` checks), lockout counter atomicity,
OIDC nonce+PKCE, secret-free logs/traces, and the new migrations' defaults all verified clean.
What Round 3 did surface is concentrated on the **billing/settlement path**: the new
outbox/reconciler machinery is well built, but the streaming settlement is not
cancellation-safe, and a few edges of the outbox design undercut the guarantees it was
built to provide.

New counts: **0 CRITICAL · 1 HIGH · 5 MEDIUM · 8 LOW**.

### Resolution status (Round 3, updated after remediation)

**Fixed & merged to `main`:**

| Finding | Fix PR |
|---|---|
| H13 — streaming settlement shielded from disconnect cancellation | #91 |
| M22 — outbox preserves the original event time | #92 |
| M23 — poison-message quarantine (attempts/last_error + migration) | #93 |
| M21 — budget gate counts spend sitting in the outbox | #94 |
| M24 — in-flight cost reservation bounds burst overshoot | #95 |
| M25 — exponential lockout + decay + admin unlock endpoint | #96 |
| L12 — Redis-outage lock no longer leaks the client | #97 |
| L17 — stale `_as_utc` comment corrected | #98 |
| L18 — `MLFLOW_METRICS_INTERVAL` validation coverage | #99 |
| L16 — README updated on shipped money controls | #100 |
| L13 — outbox guarantee level documented (at-most-once on crash) | #101 |
| L14 — unguarded reconciler documented as intentional | #102 |
| L11 — usage streamed before a provider failure is billed | #103 |

**M24** shipped as the deliberately simpler option (owner's call): an in-memory per-replica
reservation (prompt estimate + requested max-tokens ceiling) instead of a durable reservation
table; the honest residual bound is documented in `docs/usage-cost.md`. **M25** shipped as
exponential lock + decay with a platform-admin unlock (`DELETE /users/{id}/lock`) per the
owner's direction.

**Not changed (deliberate):**

- **L15** — budget compared as floats: per the finding, flag again only if invoice-grade
  accounting becomes a goal (integer micro-USD then).

### HIGH (Round 3)

#### H13 — Client disconnect mid-stream cancels the billing write: unbilled, untraced, unlogged

- **Where:** `application/completion_service.py:306-366` (`_metered_stream`), `:107-132` (`_record_usage`); trigger is Litestar's streaming response cancelling its anyio scope on `http.disconnect`.
- **Issue:** On a real client disconnect, cancellation is delivered at the provider await (`async for chunk in stream`, line 330) as `CancelledError` — a `BaseException` that the `except Exception` at line 338 does not catch, so `error` stays `None` and the `finally` proceeds to `await self._observe(...)` **inside the already-cancelled scope**. Its first checkpoint (the DB commit in `usage.record`) immediately re-raises `CancelledError`, which also sails through both `except Exception` guards in `_record_usage` (lines 115, 119). Net result: **no ledger row, no outbox row, no ERROR log, no trace**. The docstring ("a client disconnect — GeneratorExit — still records as 'ok'") and `tests/test_stream_usage_fallback.py` only cover the benign `aclose()`/`GeneratorExit` path from a non-cancelled context — the production disconnect path is the cancellation one, and it is unbilled.
- **Impact:** Any `stream: true` request whose client drops mid-stream consumes provider tokens with zero billing, zero budget consumption, and zero observability. Deliberately exploitable: read ~the whole completion, drop the connection before the final chunk → free inference. Partially reopens round-1 **C1** under client control.
- **Fix:** Shield the settlement — wrap the `finally` body (estimate + `_observe`) in `with anyio.CancelScope(shield=True):` (or run it on a shielded task with its own session). Add a regression test that iterates the stream inside a task group, cancels the scope at the provider await, and asserts a `UsageEvent` still lands.

### MEDIUM (Round 3)

#### M21 — Budget gate is blind to spend sitting in the outbox

- **Where:** `application/completion_service.py:243-259` (`_enforce_budget`) reads `usage_repository.spend_since` (`usage_repository.py:107-116`), which sums **only** `usage_event`.
- **Issue:** When ledger writes fail and events dead-letter to `pending_usage_event` (the exact failure mode the outbox was built for — a transient DB blip under load), that cost is real (already billed upstream) but invisible to every budget check until the reconciler drains it (60s cycle). During a sustained write degradation the gate under-counts for the whole degradation window.
- **Impact:** Silent budget-cap bypass precisely during the incident the outbox is designed to survive. Not covered by any test (`test_budget_enforcement.py` feeds `spend_since` directly; `test_usage_outbox.py` never asserts gate visibility).
- **Fix:** Have `spend_since` union `usage_event` with `pending_usage_event` (its `cost`/`event_created_at` columns carry what's needed), or explicitly document the gap as an accepted tradeoff in `docs/usage-cost.md` with a regression test proving the bound.

#### M22 — Reconciled usage events are re-stamped with insert time; `event_created_at` is stored but never used

- **Where:** `usage_repository.py:150-163` (reconcile insert passes no `created_at`); `orm.py:216` + `usage_repository.py:133` (outbox faithfully stores `event_created_at`, then ignores it).
- **Issue:** Events dead-lettered on July 31 and drained on Aug 1 land with `created_at` = reconcile time → the spend counts against **August's** budget window and the wrong monthly aggregate. The field that exists to prevent exactly this is dead code.
- **Fix:** Pass `created_at=row.event_created_at` in the reconcile insert (and `created_at=event.created_at` in `record` for consistency).

#### M23 — Outbox has no poison-message handling; oldest-first `LIMIT` lets poisoned rows starve newer events

- **Where:** `usage_repository.py:138-170` (`ORDER BY created_at LIMIT limit`, per-row `except → rollback`, no attempt counter); `usage_reconciler.py` (batch 200, 60s).
- **Issue:** `usage_event` has FKs on `team_id`/`api_key_id`; `pending_usage_event` has none. A team/key deleted while its events sit in the outbox makes those rows fail the ledger insert on **every** cycle, forever. Once ≥200 permanently-failing rows accumulate, the oldest-first select returns only poison — newer pending events are never selected again (silently dropped from reconciliation) while the log emits one warning per row per minute.
- **Fix:** Track `attempts`/`last_error` on the pending row and quarantine after N failures; at minimum rotate the batch window so poison can't monopolize the head.

#### M24 — Budget-gate burst overshoot is unbounded; the "bounded overshoot" docstring oversells it

- **Where:** `application/completion_service.py:243-259` (docstring + gate); streams settle only at stream end (`:341-366`).
- **Issue:** The pre-dispatch check reads committed spend with no reservation, no per-team in-flight cap, and no request-cost floor: N concurrent requests under a nearly-exhausted budget all pass. Streams widen the window from milliseconds to minutes — hundreds of long `max_tokens` streams can be admitted within one blind spot, none of which debits the window until it finishes. Overspend = in-flight × per-request cost, i.e. bounded by nothing in the code; the round-2 acceptance ("bounded overshoot, same semantics as other gateways") is only true per request, not per burst. Combined with H13, a disconnecting streamer never even settles the debit.
- **Fix (if a harder cap is wanted):** debit a pessimistic reservation (`max_tokens` × output price) at admission and reconcile at settlement, or add a per-team in-flight cap. Otherwise state the honest bound (N × cost) in the docstring and `docs/usage-cost.md`.

#### M25 — Account lockout is sustainable indefinitely by an attacker; the in-code comment claims the opposite

- **Where:** `application/user_service.py:52-57` (comment: "temporary, so an attacker can't permanently deny the victim access"), `MAX_FAILED_LOGINS = 5`, `LOCKOUT_DURATION = 15min`.
- **Issue:** The mechanics are otherwise solid (atomic in-DB counter, no re-lock during the lock, decoy-hash timing parity), but after each 15-minute expiry, 5 wrong passwords re-lock the account: **20 requests/hour from a single IP** (well under the 20/min auth rate limit) keeps a victim's password login locked forever, correct password rejected throughout. SSO is unaffected and an admin reset exists — mitigations, not prevention.
- **Fix:** Exponential lockout with cap + counter decay, or step-up/CAPTCHA instead of a hard lock after repeat cycles; at minimum correct the comment and alert on repeated lock cycles for one account.

### LOW (Round 3)

- **L11 — Mid-stream provider error discards all usage streamed before the failure** (`completion_service.py:338-344`). On a provider error the gateway emits only an error trace; tokens already streamed (paid upstream) are never billed or budget-counted. Deliberate per the docstring, but a provider dying at 95% of a long stream systematically under-bills. Not client-controllable. *Fix: on the error path, bill the estimate from `streamed_chars` — the machinery already exists two lines below.*
- **L12 — `RedisDistributedLock.hold` leaks the client if `acquire()` raises; a Redis outage silently skips rotation fleet-wide** (`infrastructure/locks.py:41-54`). Both `Redis.from_url` and `lock.acquire()` run before the `try`, so an exception there never reaches `client.aclose()`; the exception bubbles to the rotation loop, which logs and skips that day's run on **every** replica. *Fix: move `acquire()` inside the `try` (acquire-failure ⇒ `acquired=False`), and surface skipped rotations as a metric/alert.*
- **L13 — Crash between upstream success and the usage write loses the billing record** (`completion_service.py:221-241`). The outbox is a dead-letter for failed writes, not a write-ahead intent: a process kill between the provider response and `record` (ms for non-streaming; the whole settlement for streams) leaves no durable artifact. Inherent to the design — *fix: document the guarantee level ("at-most-once on crash") in the outbox docstring; a true fix is a pre-dispatch intent row.*
- **L14 — Usage reconciler runs unguarded on every replica** (`usage_reconciler.py:41-47` vs `rotation.py`'s `guarded_rotate`). Verified safe (idempotent check-insert-delete per row; the loser of a PK race rolls back), so this is wasted redundant work + DB contention scaling with replica count, and an inconsistency with the `DistributedLock` convention introduced in the same commit range. *Fix: reuse the lock port, or document that idempotency makes the lock intentionally optional.*
- **L15 — Budget limit and spend compared as floats** (`domain/entities.py:58` `limit_cost: float`; `spend_since` `SUM` of a float column vs `>=` at `completion_service.py:255`). Extends the existing `cost: float` pattern, but budgets are the first place the value *gates* a request. Drift at realistic volumes is far below enforcement granularity — flag only if invoice-grade accounting becomes a goal. *Fix (then): integer micro-USD, as `MetricsAggregator._cost_micro_usd` already does.*
- **L16 — README feature list is stale on shipped money controls** (`README.md` item 10: "*Pre-call budget enforcement is still a follow-up; streaming calls aren't token-counted yet.*"). Both claims are now false (`_enforce_budget` gates every path including streams — `test_budget_enforcement.py::test_streaming_is_gated_too`; `_metered_stream` meters usage). Service principals, scoped keys, and login lockout are also missing from the shipped list. `docs/usage-cost.md` is up to date; the front-door README is not. *Fix: update item 10 and add the new capabilities.*
- **L17 — Stale justification comment on `_as_utc`** (`application/service.py:31-33`). Says "SQLite reads timestamps back naive", but every mapped datetime column goes through Advanced Alchemy's `DateTimeUTC`, which re-attaches UTC on read (SQLite included) — the guard is defensive-only, not a live gap. *Fix: correct or drop the comment so future maintainers don't "fix" other already-safe comparisons.*
- **L18 — `MLFLOW_METRICS_INTERVAL` lacks the config-validation test its siblings have** (`config.py:213`). Logic verified fine (`minimum=0`, `0` disables the publisher via `app.py`), but it's the only numeric env var with zero coverage in `test_config.py`. *Fix: add the two cases (0 disables; negative raises).*

### Verified clean (Round 3 — checked specifically, no issue)

- **Budget authorization** — `set_budget`/`delete_budget` are platform-admin only; a team admin cannot raise their own cap. All six inference paths (chat/responses/embeddings/images + both streams) route through `_prepare` → `_enforce_budget`; no path skips the gate.
- **Service principals / scoped keys** — management-only keys rejected on `/v1/*`; `sp.enabled` re-checked independently at both the auth layer and team-management layer (disable = kill switch, regression-tested); management-scope keys mintable only for SPs; SP administration JWT-only, so a leaked key cannot self-replicate. JWT-vs-key discrimination via dot-count is safe (`token_urlsafe` alphabet has no `.`).
- **Login lockout mechanics** — atomic `UPDATE … RETURNING` counter (no cross-worker lost updates); locked accounts don't extend their own lock; decoy-hash timing parity on all no-op branches; `is_active` enforced per request.
- **Outbox idempotency & no double-billing** — one write path per request (ledger *or* outbox, never both); reconcile is check-insert-delete-commit in one transaction per row; a lost commit-ack converges without re-insert; two replicas racing the same row is safe (PK conflict → rollback → next cycle).
- **UTC consistency** — `window_start` and all timestamps aware-UTC end to end (Advanced Alchemy `DateTimeUTC` verified on SQLite and Postgres); composite `(team_id, created_at)` index backs the gate query.
- **Upstream error mapping** — uniformly applied on every dispatch path incl. mid-stream (`translate_stream`); timeout-before-connection ordering correct; no upstream bodies echoed. `InvalidBudget`/`InvalidKeyScope` fall through to the 400 default correctly; `BudgetExceeded` → 402 with the mapped handler.
- **Rotation + distributed lock** — `guarded_rotate` acquire/skip/release-on-exception correct; TTL (15 min) ≪ daily interval, so a crashed holder self-heals; no realistic two-holder overlap.
- **Observability internals** — `MetricsAggregator` counters guarded by a real `threading.Lock` across worker threads; `abandon_on_cancel=True` on blocking MLflow calls so shutdown never hangs; all four background loops (reconciler, rotation, dispatcher, metrics) start/stop symmetrically; no task leaks.
- **Hexagonal boundaries** — zero `infrastructure`/`sqlalchemy`/`litestar` imports in `domain/` and `application/` (grep-verified, including all new modules). No new string-built SQL anywhere.
- **New migrations** — lockout and scope columns default existing rows to the safe state; budget table's unique `team_id` + upsert-retry handles the concurrent-insert race.
