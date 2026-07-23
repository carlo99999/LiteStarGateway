# Plan 05 — Cross-provider failover

**Design doc:** [docs/next-steps/cross-provider-failover.md](../docs/next-steps/cross-provider-failover.md).
**Depends on:** the smart-routing candidate/router infrastructure
([docs/next-steps/smart-routing.md](../docs/next-steps/smart-routing.md)) — the
ordered candidate list + hard capability filters (`domain/routing.py:293`,
`filter_candidates`) and `RouterConfig.candidates` are the failover chain.
Request-ID drill-down in Phase 3 additionally depends on Plan 11-A correlation.
**Theme:** turn per-SDK resilience (same-provider `timeout`+`max_retries`,
`infrastructure/llm/resilience.py:17`) into **cross-provider** resilience —
when a provider is down/timing-out/429/5xx, re-dispatch to the next capable
candidate, without ever masking a client 4xx and without double-charging budget.

## Scope

A failover loop inside `CompletionService` that wraps the existing single
dispatch (`application/completion_service.py:135`, `_dispatch`) over an ordered,
capability-filtered candidate chain, for **non-streamed** calls and for
**streams before the first byte**. Reuses the router config, the §3 filters, and
the boundary error normalization (`infrastructure/llm/errors.py:109`). No changes
to native passthrough endpoints.

## Phases

### Phase 0 — Error taxonomy + eligibility classifier

- Add `is_failover_eligible(exc: DomainError) -> bool` (in `domain/failover.py` —
  reads only domain error types, honours the hexagonal boundary). Eligible:
  `UpstreamTimeout`, `UpstreamRateLimited`, `UpstreamUnavailable`,
  `UpstreamAuthFailed`. Terminal: `UpstreamRequestRejected` and every other
  `DomainError` (`domain/exceptions.py:261-290`).
- **Done when:** a table-driven unit test covers every `UpstreamError` subclass
  (eligible) and a representative terminal `DomainError` (not eligible), and the
  absolute rule "`UpstreamRequestRejected` (client 4xx) is never eligible" is
  asserted explicitly.

### Phase 1 — Sequential failover for non-streamed calls

- Build the chain from the routing decision's chosen model (attempt #1) + the
  remaining `filter_candidates` survivors in declared order. Length-1 chains
  (plain model aliases) behave exactly as today.
- Loop admit → dispatch → settle/release per attempt, bounded by `max_attempts`
  (default 3) and an overall wall-clock deadline. On a failover-eligible error:
  `release` (already in `_dispatch`'s `finally`, `:178`), advance; **no
  `settle_ok`**. On success: `settle_ok` (`:165`) bills only the serving
  provider. On a terminal error: surface immediately.
- Fix the re-admit RPM hazard (`usage_meter.py:255-256`): the key-RPM gate is
  consumed once per logical request in `_prepare` (`:509`); attempts ≥ 2 must not
  re-hit it (admit variant that skips RPM, or reserve RPM once for the chain).
- Config: per-router `failover_enabled` (default **off**), `max_attempts`,
  `overall_deadline_ms`, validated in `RouterService._validate` (`service.py:274`).
- **Done when:** a fake primary raising `UpstreamUnavailable` (503) yields a
  successful response from the second candidate; a fake raising
  `UpstreamRequestRejected` (400) surfaces immediately with **no** second
  attempt; both cases prove budget is settled at most once and never
  double-charged; and one logical request consumes exactly one key-RPM hit
  across N attempts.

### Phase 2 — Streaming failover (pre-first-byte only)

- Wrap the open + `_prime` first-chunk pull (`completion_service.py:103`, `:614`).
  A failover-eligible error at stream open **or** first `anext` (before the SSE
  `200 OK` commits, H24) → release (`:627`), retry next candidate.
- Once `_rechain` (`:86`) has yielded the first chunk, any later error
  (`translate_stream`, `errors.py:164`) is **terminal** — abort, never fail over.
- **Done when:** a fake stream that raises `UpstreamTimeout` at open succeeds on
  the second candidate with no bytes leaked to the client; a fake that raises
  **after** the first chunk aborts the connection and does **not** fail over
  (hard-boundary regression); reservations release exactly once in both paths.

### Phase 3 — Circuit breaker + observability (optional / can split)

- Observability: extend the routing decision log (`RoutingDecisionRecord`,
  `domain/routing.py:151`; persisted `service.py:1212`) with attempts, serving
  provider, per-attempt error class, and a `failover_used` flag (mirror
  `fallback_used`). Aggregate error rate, retry/failover rate, p50/p95 latency and
  breaker state per provider/model beside the existing routing-stats endpoints.
- Circuit breaker (optional): short-circuit a provider seen failing repeatedly
  for a cooldown, skipping it in the chain. Put the state machine behind a small
  port with in-memory development and Redis (`.env.sample:18`) multi-replica
  adapters.
- Console: reliability view with metadata-only aggregates and request-ID
  drill-down; never display prompt/response content or credentials.
- **Done when:** `failover_used` + attempts are persisted and queryable; the
  breaker (if built) skips a provider tripped past its threshold and re-admits it
  after cooldown; two replicas share breaker state through Redis; the console
  distinguishes errors from true zeroes.

## TDD strategy

- **Unit:** the eligibility table (Phase 0); the breaker state machine (Phase 3).
- **Integration (through the completion path):** fake `LLMGateway` whose first
  candidate raises `UpstreamUnavailable` (503) and second returns OK → request
  succeeds on candidate #2 with `failover_used=True`; the streaming analogue for
  Phase 2 (pre-first-byte).
- **Regression (the guardrails):** (1) a `400`/`UpstreamRequestRejected` is
  surfaced immediately, never retried; (2) budget is settled exactly once — a
  failed attempt reserves then releases and never bills (assert in-flight spend
  returns to baseline and total charge = serving provider only); (3) one logical
  request = one key-RPM hit across attempts; (4) mid-stream error after first byte
  does not fail over.

## Risks & mitigations

- **Double-charging budget** → per-attempt `admit`/`release` with `settle_ok`
  only on success (`usage_meter.py:239`/`:309`/`:313`); regression asserting
  single settlement. **Highest-risk item — gate Phase 1 on it.**
- **Retry storm** (SDK `max_retries` × candidates) → keep SDK `max_retries` low
  (`resilience.py:17`), bound `max_attempts` + overall deadline; failover is a
  layer *above* SDK backoff, not a third retry loop.
- **RPM double-count on re-admit** (`usage_meter.py:255-256`) → skip the key-RPM
  gate on attempts ≥ 2 (Phase 1 done-when).
- **Streaming partial output** → hard pre-first-byte boundary enforced by
  `_prime` (`completion_service.py:103`); no failover once a chunk is emitted.
- **Masking a real client error** → the absolute rule: `UpstreamRequestRejected`
  and non-upstream `DomainError` are terminal (Phase 0 test).

## Execution

- One branch per phase, TDD (RED→GREEN), full gate before every PR
  (`just test`, `just lint`, `just typecheck`, `just pre-commit`).
- Hexagonal boundary is law: the classifier lives in `domain/`; the loop and
  config live in `application/`; no new `infrastructure` imports from
  `domain`/`application`.
- Phases 0→1 are the shippable core (sequential failover for non-streamed calls);
  Phase 2 is an independent slice on the streaming open path; Phase 3 splits into
  observability (do first, cheap) and the optional breaker (defer unless the
  failover-rate metric justifies it).
- Verify the merged result, not just the branches — the budget invariant only
  holds end-to-end on merged `main`.
