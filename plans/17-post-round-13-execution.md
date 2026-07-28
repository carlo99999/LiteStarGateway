# Plan 17 — Post-Round-13 execution: multi-replica money, decimal, guardrails

Plan 15's Phase C sequence (C1–C5) is **exhausted**: Responses tool events,
failover, response caching, budget alerts and usage analytics all shipped, and
the Round 13 findings are closed ([16-round-13-remediation.md](16-round-13-remediation.md)).
This plan owns what comes next and in which order.

It **references** existing design docs instead of restating them:

- Slice 2 and 4 execute [13-billing-integrity.md](13-billing-integrity.md)
  Phases 3 and 2, which exist as four bullets each — the detail they lack is
  written here;
- Slice 5 executes [06-guardrails.md](06-guardrails.md), which is already
  phase-level detailed — only entry criteria and sequencing are added here.

Ground rules (unchanged from Plans 14–16): one reviewable PR per slice; every
PR carries a regression that **fails before the fix**; full gate before each
(`uv run pytest -q --cov-fail-under=80`, `ruff check`, `ruff format --check`,
`pyrefly check`, `pre-commit run --all-files`), plus `just test-postgres` and
`just migration-check` for anything touching Alembic.

## Why this order

The gateway is built to scale out — Redis adapters exist for the response
cache, the rate limiter and the circuit breaker, and after #396 the alert
dispatcher is multi-replica safe. **The budget gate is the last per-process
component**, and it is the one that guards money:

```python
class InFlightSpend:
    """...In-memory and per-process — replicas don't see each other's
    in-flight spend, so the overshoot bound is per replica, not global."""
```

That is the same class as three of the Round 13 MEDIUMs: a constraint the
product declares and the runtime does not enforce fleet-wide. It goes first.
Decimal money follows because it is a precision risk on the same path, and it
is cheaper to decimalize once the reservation arithmetic has settled than to do
both at the same time. Guardrails come third: the largest *product* gap, and
the only planned feature that opens a market rather than refining what exists.

| # | Slice | PRs | Size | Depends on |
|---|-------|-----|------|-----------|
| 0 | Test-harness hardening | 1 | ~0.5 d | — |
| 1 | Redis required + real Redis in CI | 1 | ~0.5 d | — |
| 2 | Console reliability view (Plan 05 tail) | 1 | ~0.5 d | — |
| 3 | Distributed budget reservations (Plan 13 Ph. 3) | 4 | ~4 d | 0, 1 |
| 4 | Allowed-hosts middleware | 1 | ~0.5 d | — |
| 5 | Decimal money (Plan 13 Ph. 2) | 3 | ~5 d | 3 |
| 6 | Guardrails (Plan 06, its own Ph. 0–3) | 4 | ~6 d | — |
| 7 | Per-key budgets (Plan 13 Ph. 4) | 2 | ~2 d | 3, 5 |

Slices 2 and 4 are independent fillers — land them whenever a review is
blocking. See [Parallelism](#parallelism) for what can actually run at the same
time: the dependency column understates the real constraint, which is file
ownership, not logical order.

## Decisions taken up front

Two questions came up while writing this plan and are settled here so nobody
re-opens (or re-measures) them.

### Redis is required outside local development

Today `app.py:420` logs a WARNING when a production deployment has no
`REDIS_URL`, and every shared component silently falls back to a per-process
implementation: rate limiter, circuit breaker, exact-match cache, semantic
cache. That fallback is not a feature — it is how a rate limit becomes
"per replica × instance count", and it is the direct cause of ISSUE-029 (the
Redis breaker had diverged from the in-memory one and no test could see it).

**Decision:** missing `REDIS_URL` outside local becomes a startup failure,
alongside the existing PostgreSQL and secure-cookie checks. Consequences:

- the in-memory adapters **stay**, but as what they actually are — local
  development only, unreachable in any deployed environment. Deleting them
  would touch ~1876 tests to buy nothing;
- **no new component ships two implementations** unless one shared,
  parametrized conformance suite covers both. That is *less* test code than
  today's pattern (the breaker has one test file per adapter), and it is the
  device that was missing when ISSUE-029 slipped through;
- **CI runs a real Redis.** The reservation store's atomicity lives in a Lua
  script, and a Lua script verified against a Python fake proves nothing about
  Redis semantics — that is ISSUE-029's lesson at second hand. Six lines of
  `services:` in the existing job, plus a `just test-redis` mirroring
  `just test-postgres`.

### The PostgreSQL test suite stays as slow as it is

SQLite is already refused in production by `config.py` ("Production requires
PostgreSQL"), so nothing has to change for correctness. Moving the default
developer loop to PostgreSQL costs, measured on this schema (25 tables):

| Per-test database cost | Measured |
|---|---|
| `CREATE DATABASE` | 19 ms |
| `create_all` | 82 ms |
| `DROP DATABASE ... WITH (FORCE)` | 117 ms |
| **today, per test** | **218 ms** |
| alternative: shared database + `TRUNCATE ... RESTART IDENTITY CASCADE` | **14 ms** |

Full suite: **189 s on SQLite, 240 s on PostgreSQL** — the extra 51 s implies
roughly 250–270 of the 1876 tests actually touch a database.

**Decision: accept the 51 seconds.** A shared-database refactor is ~1 day with
real flakiness risk (module-level caches that outlive a `TRUNCATE`, isolation
that silently depends on the table list staying complete — the same class as
ISSUE-030) to buy one minute. Not a trade worth making now.

Recorded for whoever hits the wall later, so the measurement is not repeated:
the fix is one shared database created per session plus `TRUNCATE` per test —
**not** a transaction-per-test rollback, which would invalidate the
multi-replica tests that need two independent sessions to see each other's
commits (#396, and more of them arrive with Slice 3). Free first step, worth
taking whenever the container definition is touched anyway: `fsync=off`,
`synchronous_commit=off`, data directory on tmpfs. Revisit the refactor when
the PostgreSQL suite passes ~6 minutes locally.

## Parallelism

Two hard rules first, because breaking either costs more than the parallelism
buys:

1. **One Alembic migration in flight at a time.** Slices 5 and 7 are the only
   ones touching `migrations/`; two open branches produce two heads and a merge
   conflict nobody enjoys resolving. This bit us once already in Round 13,
   where PR 1 and PR 6 had to be serialised for exactly this reason.
2. **One owner at a time for `completion_service.py` and `usage_meter.py`.**
   Slice 3 rewrites what `_meter.admit` returns at six call sites (`:679`,
   `:714`, `:790`, `:825`, `:961`, `:1175`) and threads a handle through
   `_dispatch`; Plan 06's pre-call hook goes **immediately before those same
   lines**. Concurrent branches there conflict on every hunk.

### Collision map

| File / area | Slices that touch it |
|---|---|
| `application/completion_service.py` | 3, 6b |
| `application/usage_meter.py` | 3, 5, 7 |
| `persistence/orm.py` + `migrations/` | 5, 7 |
| `app.py`, `config.py` (additive wiring) | 1, 3, 4, 6b, 7 |
| `ui/src/` | 2, 6 (phase 3), 7 |
| everything else | disjoint |

### The unlock: split Slice 6

Plan 06's Phase 0–2 are mostly **new files** — `domain/guardrails.py`,
`application/guardrails/{service,pii_regex,moderation,webhook}.py` — with a
one-line addition to `exceptions.py`. Only the hook wiring touches
`completion_service.py`. So:

- **6a** (port, verdict types, registry, chain runner, PII detectors,
  moderation provider, their unit tests): zero overlap, startable **today**;
- **6b** (the two hook points, error mapping, console config): after Slice 3
  merges.

The same trick does *not* work for Slice 5: its first PR decimalizes
`domain/pricing.py`, which `usage_meter._reservation_cost` calls, so it wants
the hot path to have settled.

### Tracks

- **Track A — hot path, strictly serial, one owner:** 0 → 1 → 3 → 5 → 7.
- **Track B — anything, any owner, any time:** 2, 4, 6a.
- **Track C — gated:** 6b once 3 is merged; Plan 06 phase 3's console after 2.

Two people is the useful maximum: a third would spend its time queued behind
`completion_service.py`. With two, ~19.5 days of work lands in ~12 elapsed days —
Track A is the critical path and nothing shortens it.

**With one person** the picture is different: what parallelises is not the work
but the **wait**. Each PR costs ~11 minutes of CI (`checks` and `postgres` run
~10–11 min each), and that window is enough to branch and write the next
slice's failing tests — which is how the Round 13 remediation ran eleven PRs in
one sitting. Keep at most two branches alive, and never two that share a file
from the collision map.

---

## Slice 0 — Test-harness hardening (1 PR, ~0.5 d)

Round 13 produced two defects that the suite could not have caught, both from
the same cause: **a fake that is more forgiving than the thing it stands for**.

- `FakeRedis` recorded TTLs and never applied them, keeping a broken circuit
  breaker green (ISSUE-029);
- the multi-replica cache test gave each "replica" a freshly-minted `Model`,
  something production never does (found while fixing ISSUE-023a);
- the alert duplication (ISSUE-026) only reproduces with two independent
  sessions on one database plus a barrier inside the send.

Each was fixed in place. This slice extracts the three tools so the next
occurrence is cheap to test, **before** Slice 2 needs all three at once.

**Deliverables** — `tests/support/` (new package, importable like
`_invite_helpers`):

- `MutableClock`: advanceable clock, already written twice by hand;
- `FakeRedis`: the TTL-faithful fake from `tests/routing/test_circuit_breaker.py`,
  moved and extended with the commands the reservation store will need
  (`eval`/`evalsha` or a scripted-hash stub, `hset`, `hdel`, `hgetall`);
- `two_sessions` fixture: two `AsyncSession`s over one SQLite file, from
  `tests/budgets/test_budget_alert_dispatch.py`.

**Done when:** the existing circuit-breaker, semantic-cache and alert-dispatch
tests import from `tests/support/` and stay green with no behavioural edits.

**Risk:** a shared fake drifts from real Redis over time. Mitigation: it stays
a *strict* fake — an unimplemented command raises rather than returning `None`.

---

## Slice 1 — Redis required + real Redis in CI (1 PR, ~0.5 d)

Executes the decision above. Small and deliberately early: Slice 3 depends on
it, because it is what allows the reservation store to treat Redis as present
rather than optional.

- `config.py`: `InsecureConfigurationError` when `redis_url` is unset and the
  environment is not local, replacing the `app.py:420` warning. The existing
  `tests/config/test_startup_warnings.py` cases invert from "warns" to
  "refuses to start";
- `.github/workflows/ci.yml`: a `redis:7` service on the job that will run the
  reservation tests, mirroring the existing `postgres:17` service block;
  `just test-redis` for the same thing locally. `docker-compose.yml` and
  `docker-compose.dev.yml` already run Redis and already export `REDIS_URL`, so
  nothing changes for the running stack;
- docstrings on the four in-memory adapters restated: local development only,
  not a production fallback — which the config check now makes true.

**Done when:** a staging/production `Settings` without `REDIS_URL` fails at
startup with a message naming the variable, and the suite has a job where a
real Redis is reachable.

## Slice 2 — Console reliability view (1 PR, ~0.5 d)

The last open item of [05-cross-provider-failover.md](05-cross-provider-failover.md).
Breaker state and failover attempts are persisted (`routing_decision.attempts`,
`failover_used`) and, after #399, the breaker's state machine is finally
correct — but nothing surfaces it. Read-only panel: attempts distribution,
failover rate per router, currently-tripped candidates.

Deliberately before the big slices: it makes work already paid for visible, and
it is a natural pause between two heavy refactors.

---

## Slice 3 — Distributed budget reservations (4 PRs, ~4 d)

Executes [13-billing-integrity.md](13-billing-integrity.md) Phase 3. The design
doc gives the shape (`BudgetReservationStore`, server-generated UUID, TTL for
process death); the two things it does not address are the hard parts, so they
are decided here.

### Problem 1 — `release()` is synchronous, and one caller cannot await

`UsageMeter.release()` (`application/usage_meter.py:473`) is a plain `def`. Most
of its eleven call sites in `completion_service.py` sit in `async` bodies and
can simply `await`. Three cannot:

```python
def release() -> None:            # completion_service.py:741, :853, :1333
    nonlocal released
    if not released:
        released = True
        self._meter.release(team_id, reservation)

weakref.finalize(gen, release)    # :757, :869, :1359
```

`weakref.finalize` is a **garbage-collection callback**: it may run with no
running event loop, and it cannot await. It exists for the
drop-before-first-byte case — a client that opens a stream and disappears.

**Decision.** The reservation TTL *is* the answer for that path, exactly as the
design doc intends it for process death:

- every normal path becomes `await meter.release(handle)` — an explicit HDEL,
  immediate;
- the finalizer keeps a synchronous best-effort: if a loop is running, schedule
  the release with `loop.create_task`; otherwise do nothing and let the TTL
  reclaim it. Documented as such, not silently.

This is a real, bounded weakening: a dropped stream can hold its reservation
until the TTL expires instead of releasing at GC time. That is strictly better
than today's behaviour on a *crashed replica*, which leaks the reservation
forever (in-process state dies with the process while the ledger keeps the
committed spend).

### Problem 2 — the gate must stay atomic across replicas

Today's comment is the invariant to preserve:

```python
# No await between reading the in-flight total and adding the new
# reservation: concurrent gates interleave only at checkpoints, so
# two requests can't both read the same total and slip through.
```

Distributed, that becomes one round trip that reads, decides and writes. A Lua
script keyed per team:

```text
KEYS[1] = budget:res:{team_id}          -- hash: reservation_id -> "amount:expires_at"
ARGV    = now, spent, limit, amount, ttl, reservation_id

1. HGETALL; drop and HDEL fields whose expires_at <= now (opportunistic sweep)
2. reserved = sum(remaining amounts)
3. if spent + reserved >= limit: return {0, reserved}          -- refuse
4. HSET reservation_id = "amount:now+ttl"; EXPIRE KEYS[1] ttl_max
5. return {1, reserved}
```

`spent` still comes from PostgreSQL and is passed in: settlement stays
authoritative, Redis only bounds the concurrent window — the design doc's own
framing. The whole-key `EXPIRE` prevents a team that stops sending traffic from
leaving a hash behind forever.

Release is `HDEL` by reservation id: **idempotent**, which also removes today's
double-release hazard (a float amount subtracted twice silently under-counts;
deleting a field twice does nothing).

### PR breakdown

| PR | Content |
|----|---------|
| 3-1 | `domain/ports/budget_reservation.py`: `Reservation` (frozen: id, team_id, amount) + `BudgetReservationStore` protocol (`try_reserve`, `release`). In-memory adapter reproducing the Lua semantics including expiry. Pure unit tests. No wiring. |
| 3-2 | Redis adapter with the script above, `build_budget_reservation_store(settings)` mirroring `build_rate_limiter` (`infrastructure/rate_limiter.py:108`) and `build_circuit_breaker`. Tests against the TTL-faithful fake from Slice 0 plus an opt-in real-Redis test. |
| 3-3 | `UsageMeter.admit` returns a `Reservation`; `release` becomes `async` and takes it. All `completion_service.py` call sites threaded through, including the three finalizer closures with the documented best-effort path. `InFlightSpend` deleted. |
| 3-4 | Wiring in `app.py`/`api_router/dependencies.py` (replacing the module-level `_in_flight_spend` singleton at `dependencies.py:84`), settings key `BUDGET_RESERVATION_TTL_SECONDS` (default 300), operations doc. |

### Tests

- **two replicas, one store**: both admit against a budget with room for one →
  exactly one `BudgetExceeded`. This is the test that fails today;
- expired reservation is swept and its headroom returned;
- release is idempotent (twice → same remaining headroom);
- a dropped stream's reservation is reclaimed by TTL (no finalizer run);
- **no-Redis parity**: the in-memory adapter passes the identical suite, so a
  single-replica deployment behaves exactly as before;
- streaming settlement still releases exactly once (existing suite must stay
  green untouched — it is the regression net for the refactor).

### Risks

- **Blast radius.** PR 3-3 touches the money path in eleven places. Mitigation:
  3-1/3-2 land the store with zero production wiring, so 3-3 is a mechanical
  swap reviewable against a green suite.
- **Redis down.** Decide explicitly and test it: a store error **fails closed**
  (refuse admission) rather than admitting unbounded spend. Different from the
  cache, where an error is a miss — this one guards money.
- **TTL too short** cancels a live long stream's protection. 300 s default,
  configurable; long streams re-reserving is out of scope and noted.

---

## Slice 4 — Allowed-hosts middleware (1 PR, ~0.5 d)

Deliberately excluded from #398 as wider than the finding. With SSO now
requiring a fixed callback outside local, the remaining `Host`-derived surface
is local development and any future request-derived URL. A standard allowed-hosts
middleware fed by a settings list, mandatory outside local, closes the class
rather than the instance.

---

## Slice 5 — Decimal money (3 PRs, ~5 d)

Executes [13-billing-integrity.md](13-billing-integrity.md) Phase 2 and retires
the R3-L15 deferral that has survived every round since Round 3. Phase 1 built
the seam on purpose: `compute_cost`/`RateCard`/`BillableUsage` are the only
place money is calculated.

**Migration surface** (verified against the ORM metadata, `Float` columns only;
`routing_decision.score`/`decision_ms` are not money and stay `float`):

| Table | Columns |
|---|---|
| `model` | `input_cost_per_token`, `output_cost_per_token`, `cache_write_cost_per_token`, `cache_read_cost_per_token`, `image_cost_per_image`, plus the JSON `image_prices` values |
| `usage_event`, `pending_usage_event` | `cost` |
| `team_budget` | `limit_cost` |
| `pending_budget_alert` | `spend`, `limit_cost` |
| `routing_decision` | `chosen_input_cost`, `chosen_output_cost`, `alt_input_cost`, `alt_output_cost` |

**Decisions to make in PR 5-1, not later:**

- **scale and rounding, once, in `domain/money.py`**: proposal `NUMERIC(20,10)`
  for rates (per-token prices run to 7–8 significant decimals) and
  `NUMERIC(20,6)` for costs, limits and aggregates, `ROUND_HALF_UP` at the
  single point where a computed cost is persisted;
- **the API boundary stays `float`.** DTOs keep JSON numbers, so the OpenAPI /
  TypeScript schema-drift gate and the console are untouched; `Decimal` is
  authoritative in the domain and at rest. Revisit only if a customer needs
  exact strings — recorded as the known limit;
- **SQLite.** It has no native NUMERIC affinity; SQLAlchemy stores `Decimal` as
  string or float depending on the type used. Pick `sa.Numeric(asdecimal=True)`
  and **verify on both dialects** that a round-trip is exact — this is the part
  most likely to bite, and it must be a test, not an assumption.

| PR | Content |
|----|---------|
| 5-1 | `domain/money.py` (scale, rounding, parse/format helpers) + `pricing.py` decimalized in place. Exact-value unit tests replacing `pytest.approx`. No persistence change. |
| 5-2 | ORM columns → `Numeric`, Alembic migration rehearsed on PostgreSQL **and** SQLite, repository conversions at the boundary, `float(...)` at the DTO edge. |
| 5-3 | Aggregates: `spend_since`, savings, analytics timeseries — assert order-independence (the Phase 2 "done when": repeated aggregation gives the identical total regardless of row order). |

**Risk.** The migration rewrites the largest tables (`usage_event`). Rehearse on
a seeded copy and measure; if it is slow, the plan becomes add-column /
backfill / swap rather than `ALTER TYPE`.

---

## Slice 6 — Guardrails (4 PRs, ~6 d)

Executes [06-guardrails.md](06-guardrails.md) as written — Phase 0 (port,
verdicts, hook points with an empty chain), Phase 1 (regex/rule PII), Phase 2
(LLM/native moderation), Phase 3 (webhook provider + console config). Nothing
to add to its design; only the entry criteria:

- starts once Slice 2 is merged, because its pre-call hook sits immediately
  before `_meter.admit` and both edit the same window of `_prepare`;
- Phase 0 must leave the full suite green with **no** policy configured — the
  off-by-default guarantee is the review gate;
- the webhook provider reuses the SSRF-guarded client from the budget-alert
  channel rather than growing a second egress path.

---

## Slice 7 — Per-key budgets (2 PRs, ~2 d)

[13-billing-integrity.md](13-billing-integrity.md) Phase 4. Waits for Slice 2
(one admission path, two policies — it must reserve against both scopes in the
same atomic step, which only exists after the store) and Slice 4 (a second
limit comparison in float is a second drift source). Reuses Plan 07's threshold
semantics rather than a parallel calculation.

## Non-goals for this plan

- **Plan 08 (audio, moderations, rerank, Batch/Files)** — surface breadth,
  unblocked by Phase 1 pricing but not valuable before the above;
- **Plan 12 (routing evolution)** — refinement of a working router;
- **the granian swap** — parked by Plan 15 for a measured 5–15% in a profile
  already declared diffuse.
