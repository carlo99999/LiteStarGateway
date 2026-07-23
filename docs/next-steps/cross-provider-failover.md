# Implementation prompt: Cross-provider failover

> **Status — not started.** Design only. Builds directly on the smart-routing
> candidate/router infrastructure (`docs/next-steps/smart-routing.md`); read that
> first. Execution plan: `plans/05-cross-provider-failover.md`.

You are adding **cross-provider failover** to this LLM gateway (Litestar,
hexagonal, `src/litestar_gateway/`). Today resilience is *per-SDK only*: the
OpenAI/Anthropic clients are configured with `timeout` + `max_retries`
(`infrastructure/llm/resilience.py:17`, default `timeout=60.0`, `max_retries=2`)
which retry the **same** provider. There is no path that, when a provider is
down / timing out / rate-limited / 5xx, re-dispatches the request to a
**different** candidate model on a **different** provider. This feature adds that.

Read `CLAUDE.md`, `CONTRIBUTING.md`, and `domain/ports.py` first; keep the
existing conventions (Protocol ports in `domain/`, services in `application/`,
adapters in `infrastructure/`, DI, full typing, frozen dataclasses, many small
files, surgical changes, TDD).

---

## 1. Relationship to smart routing (non-negotiable reconciliation)

Smart routing's §4 failure policy and failover are **different mechanisms at
different points in the request lifecycle** — do not conflate them:

- **Routing fallback (existing, `application/routing/service.py:744`,
  `_run_strategy`):** happens *before dispatch*. A strategy exception/timeout
  only **rewrites the model name** to `default_model` (or the first capable
  candidate). The request has not yet been sent to any provider.
- **Failover (new):** happens *after dispatch*, when the actual provider **call
  fails**. The chosen model is fine; the upstream is not. Failover must try the
  **next candidate** and re-dispatch.

The two share the same raw material — the router's ordered, capability-filtered
candidate list. A **failover chain** is exactly the ordered list of candidates
that survive the §3 hard filters (`domain/routing.py:293`, `filter_candidates`),
so any fallback the gateway picks can actually serve the request (vision / tools
/ json_schema / context-window). Failover reuses the router config
(`RouterConfig.candidates`, `domain/routing.py:69`) and the filter — it does not
introduce a second candidate concept.

Ordering of the chain: the routing decision's chosen model is attempt #1; the
remaining capable candidates follow **in declared order** (deterministic, same
convention as the §4 fallback at `service.py:1000`). A plain (non-router) model
alias has a chain of length 1 → no failover, behaviour identical to today.

---

## 2. Error taxonomy — failover-eligible vs terminal (the core rule)

Errors are already normalized at the boundary
(`infrastructure/llm/errors.py:109`, `translate_upstream_error`). Failover keys
off the **domain** error type, never a raw SDK exception:

| Domain error (`domain/exceptions.py`) | Source (`errors.py`) | HTTP | Failover? |
|---|---|---|---|
| `UpstreamTimeout` | `errors.py:113` (timeout types) | 504 | **YES** |
| `UpstreamRateLimited` (429) | `errors.py:118` | 429 | **YES** |
| `UpstreamUnavailable` (5xx / unreachable) | `errors.py:122`, `:136` | 502 | **YES** |
| `UpstreamAuthFailed` (401/403 gateway credential) | `errors.py:130` | 502 | **YES** (this candidate's credential is bad; another provider may work) |
| `UpstreamRequestRejected` (other 4xx) | `errors.py:134` | 400 | **NO — terminal** |
| any `DomainError` (`BudgetExceeded`, `RateLimited`, `ModelNotFound`, …) | pre-dispatch | — | **NO — terminal** |

**The absolute rule:** a client-caused `4xx` is **never masked** by failover. A
`400` (out-of-range param, malformed body) is the caller's fault and will fail
identically on every provider — retrying it wastes budget/latency and hides the
real error. `UpstreamRequestRejected` and any non-upstream `DomainError` surface
**immediately, unchanged**. Only the transient/provider-down bucket
(`UpstreamTimeout`, `UpstreamRateLimited`, `UpstreamUnavailable`,
`UpstreamAuthFailed`) advances the chain.

Implement the classifier as one pure predicate,
`is_failover_eligible(exc: DomainError) -> bool`, unit-tested against a table of
every `UpstreamError` subclass plus a representative terminal `DomainError`. Keep
it beside the taxonomy it depends on (a small `domain/failover.py` or
`infrastructure/llm/failover.py` — it reads only domain error types, so `domain/`
is preferred to keep the hexagonal boundary clean).

---

## 3. The failover loop (where it lives)

Insert the loop in `CompletionService`, wrapping the per-attempt dispatch that
`_dispatch` already performs (`application/completion_service.py:135`). Today
`_prepare` (`:497`) resolves **one** model, admits budget, and returns it;
`_dispatch` runs the single call. Failover generalizes this to *N* attempts over
the chain, each attempt being one full admit → call → settle/release cycle.

Bounds (all configurable, all with safe defaults):

- **max attempts:** default 3 (chosen + up to 2 fallbacks). Never unbounded.
- **per-attempt timeout:** the existing SDK `timeout` (`resilience.py:17`) already
  bounds one call; failover adds no new per-attempt timer beyond it.
- **overall deadline:** a wall-clock budget for the whole chain (default ~90 s)
  so a slow-timeout × many-candidates chain cannot exceed the caller's patience;
  once exceeded, stop and surface the last error.

Interaction with per-SDK retries — **avoid a retry storm.** The SDK
`max_retries=2` (`resilience.py:17`) handles *same-provider transient blips*
(one 429/503 spike); failover handles *provider-down*. Multiplying them
(`max_retries × candidates`) is a self-inflicted amplification. Guidance: keep
SDK `max_retries` **low** (1–2) now that a second provider absorbs sustained
failure, and document that failover is not a substitute for SDK backoff but a
layer above it. Do **not** add a third retry layer.

---

## 4. Budget / metering correctness across attempts (must not double-charge)

This is the correctness crux. The money flow today (per call):

- `admit` (`usage_meter.py:239`, called from `_prepare:567`) reserves the
  **pessimistic** cost of *that model* and returns the reservation amount.
- `_dispatch` (`completion_service.py:135`) runs the call; on success it calls
  `settle_ok` (`:165` → `usage_meter.py:313`) which **bills actual usage**; on
  failure it calls `trace_error` (`:160`) and **does not settle**; either way the
  reservation is released in the `finally` (`:178` → `usage_meter.py:309`).

The invariant to preserve: **a failed attempt reserves, then releases, and never
settles → it charges nothing.** Only the *succeeding* provider reaches
`settle_ok`. Therefore the failover loop must, per attempt:

1. `admit` against **that attempt's** model (pricing differs per candidate, so
   the reservation must be re-taken per attempt — `_reservation_cost` reads the
   model's own rates).
2. dispatch; on a failover-eligible error → `release` (already in `_dispatch`'s
   `finally`) and advance; **no `settle_ok`**.
3. on success → `settle_ok` bills only this provider, then `release` the
   reservation. Exactly one settlement for the logical request.

**Re-admit hazard (call out explicitly):** `admit` also runs the team+key RPM
gates (`usage_meter.py:255-256`). The key RPM gate is *already* consumed once
per logical request in `_prepare` (`enforce_key_rate_limit`,
`completion_service.py:509`). Re-invoking full `admit` per attempt would
double-count RPM and could spuriously 429 a caller mid-failover. Resolve by
either (a) an `admit` variant that skips the RPM gate on attempts ≥ 2, or (b)
reserving RPM once for the chain and only re-doing the *budget* reservation per
attempt. Pick one and test it (a regression asserting N attempts consume exactly
one key-RPM hit).

---

## 5. Streaming failover — the hard boundary

Streaming failover is allowed **only before the first byte is emitted to the
client.** Once a single chunk has been handed downstream, you cannot fail over —
the SSE `200 OK` is committed and partial tokens are already on the wire.

The existing machinery makes the boundary precise:

- `open_chat_stream` (`completion_service.py:614`) opens the provider stream,
  then `_prime` (`:103`) pulls the **first chunk** *before* the controller
  commits the SSE response (H24). An error at open **or** at first-chunk is raised
  here, synchronously, before any byte reaches the client.
- **Failover-eligible zone:** the `astream_*` open call plus that first `anext`
  in `_prime`. If either raises a failover-eligible domain error, release
  (`:627`) and retry the next candidate — the client has seen nothing.
- **Terminal zone:** once `_rechain` (`:86`) yields the first chunk, any later
  error comes through `translate_stream` (`errors.py:164`) mid-stream and is
  **terminal** — the connection can only be aborted, never failed over. Document
  this as a hard, non-negotiable boundary.

Non-streaming calls (`chat_completion`, `responses`, `embeddings`, `images`)
have no such boundary — the whole response is buffered, so failover applies to
the entire call.

Native passthrough endpoints (`native_messages`, `generate_content`) meter
natively and are provider-specific by contract (`ProviderMismatch`,
`completion_service.py:298`); **failover is out of scope for native endpoints**
in this feature — they name one concrete same-protocol model.

---

## 6. Idempotency & observability

- **Idempotency:** failover re-sends a request that may have partially executed
  upstream (a provider that 504s may still have started generating). For chat
  completions this is acceptable (no external side effect beyond token spend,
  which we do not settle on the failed attempt). Do **not** fail over tool/
  function *executions* — the gateway only relays tool *definitions*, so this is
  a documentation note, not code. State the assumption: failover is safe because
  a failed attempt is never settled and completions have no gateway-side effect.
- **Observability:** mirror the routing decision log (`RoutingDecisionRecord`,
  `domain/routing.py:151`; persisted at `service.py:1212`). Record per request:
  the ordered attempts, each attempt's provider/model and its terminal error
  class, which provider ultimately **served** the response, and a
  `failover_used` boolean (analogous to `fallback_used`). Surface `failover_used`
  and `attempts` on the completion for admin visibility, and expose an aggregate
  ("failover rate per provider") alongside the existing routing stats endpoints.
- **Reliability/SLO view:** aggregate per provider/model error rate, retry and
  failover rate, p50/p95 latency and breaker state. Keep it metadata-only and
  allow request-ID drill-down to logs/traces; never expose prompt/response bodies.
  The drill-down depends on Plan 11-A request correlation.

## 7. Circuit breaker (later phase / partial non-goal)

An optional short-circuit: a provider observed failing repeatedly is marked
"open" for a cooldown and **skipped** in the chain (removed as attempt #1) to
avoid hammering a known-down upstream. This is **Phase 3, optional** — it needs
shared cross-request state (in-process counter, or Redis for multi-replica, which
the project already wires optionally, `.env.sample:18`). Ship sequential failover
first; add the breaker only if the failover rate metric shows it is worth it.
Use a small state port so in-memory development and Redis-backed multi-replica
production share the same state machine and test contract.

## 8. Config surface & console exposure

- Per-router config (lives in `RouterConfig.strategy_config` or a sibling
  `failover` block, validated in `RouterService._validate`,
  `service.py:274`): `failover_enabled` (default **off** — opt-in, so existing
  routers are unchanged), `max_attempts`, `overall_deadline_ms`.
- No new global env var is required; the SDK `REQUEST_TIMEOUT`/`MAX_RETRIES`
  knobs (`.env.sample:20`) stay as the per-attempt bound. Document the
  recommended `MAX_RETRIES` reduction (§3).
- Admin UI (Plan 03): show the failover chain a router would use and the
  provider/model error rate, retry/failover rate, p50/p95 latency and breaker
  state; editing the failover block is a later increment.

## 9. Explicit non-goals

- No failover on native `/v1/messages` or `generateContent` endpoints (§5).
- No mid-stream failover after first byte (§5) — hard boundary.
- No hedged/parallel dispatch (racing two providers at once) — sequential only.
- No failover of client `4xx` (`UpstreamRequestRejected`) or any non-upstream
  `DomainError` — the absolute rule (§2).
- No distributed breaker without Redis; single-process deployments may use the
  in-memory adapter (§7).
- Adaptive/latency-weighted provider ranking — future work only.
