# Plan 04 — Response Caching

**Design doc:** [`docs/next-steps/response-caching.md`](../docs/next-steps/response-caching.md)
**Depends on:** the money core (`application/usage_meter.py`) and the request
pipeline (`application/completion_service.py`) as they stand today; the semantic
tier depends on the S3 embedding path (`application/routing/embeddings.py`,
shipped). Redis is optional (in-memory fallback), same as rate limiting.
**Theme:** a gateway-wide response cache — exact-match first, semantic opt-in —
that returns a stored provider response for an equivalent request. Off by
default, opt-in per team/model, metered at $0. The #1 cost saver.

## Scope

A `ResponseCache` domain port with two tiers behind it, wired into the single
integration point in `_dispatch` (`completion_service.py:158`/`:165`):

- **Cache-key derivation** — pure, canonical, tenant-scoped (design §2, §3).
- **Exact-match tier** — Redis-backed (in-memory LRU fallback), TTL + bounded.
- **Semantic tier** — reuses the S3 `EmbedFn`; per-team opt-in; threshold 0.97.
- **Metering** — a hit settles through `UsageMeter` at zero provider cost with a
  `cache_hit` marker; the reservation releases as today.
- **Observability** — hit rate + cost-saved endpoint mirroring routing savings.
- **Config** — global kill-switch + per-team/per-model toggle + console surface.

Reconciles with `smart-routing.md` §8: the per-router semantic-cache flag becomes
a thin caller of this service, sharing the store and `EmbedFn` (no parallel cache).

## Phases

### Phase 0 — Exact-match, in-memory (1 slice)

- `domain/ports/response_cache.py` (Protocol), `CacheKey`/`CachedResponse` frozen
  dataclasses, and the pure key-derivation function (design §2).
- In-memory `OrderedDict` LRU adapter (`infrastructure/cache/`), TTL-aware, the
  eviction pattern from `embeddings.py:126`.
- Wire lookup before `await call()` (`completion_service.py:158`) and write after
  `settle_ok` (`completion_service.py:165`), gated to non-streamed
  `chat.completions`/`responses`; a `cache_hit` settlement path in `UsageMeter`
  that bills the stored token counts at `cost=0.0`.
- Global `RESPONSE_CACHE_ENABLED` (default off) + per-team/per-model toggle.
- **Done when:** with the flag on, a repeated identical chat request returns the
  stored body without a provider call and records a `cache_hit` usage event at
  $0; with the flag off, behavior is byte-identical to today.

> **Done — 26 July 2026.** Shipped as-designed: `domain/ports/response_cache.py`
> (`ResponseCache` Protocol, frozen `CacheKey`/`CachedResponse`) and the pure
> `domain/response_cache_key.py` (`derive_cache_key`/`is_cacheable`);
> `infrastructure/cache/in_memory.py`'s `OrderedDict`-LRU, TTL-aware adapter,
> selected by `RESPONSE_CACHE_ENABLED` (default off) via `build_response_cache`;
> wired into `CompletionService._dispatch` at the two designed insertion points
> (lookup before `call()`, write after `settle_ok`), gated to non-streamed
> `chat.completions`/`responses` only. `UsageMeter.settle_cache_hit` bills the
> *stored* token counts at a hard `cost=0.0` (never `_parse_usage`) and stamps
> `cache_hit=True` on both `UsageEvent` and `TraceRecord`. The per-team/model
> toggle landed as two columns on the existing `Model` entity/table
> (`cache_enabled`, `cache_allow_nondeterministic`) rather than a new table —
> Model is already the natural team+model-scoped unit — surfaced through the
> existing model create/update endpoints (the console checkbox itself is
> Phase 3). The `cache_hit` column shipped on both `usage_event` and
> `pending_usage_event` (the outbox) in one migration
> (`2026-07-26_add_response_cache_columns_bdc8a2e4eb0e`), keeping one Alembic
> head. Two deliberate simplifications versus the letter of the design: (1) the
> cross-provider failover retry path (Plan 05) does not participate in the
> cache — each retry targets a different candidate model, and per-attempt keys
> are deferred; (2) a response whose usage can't be read cleanly is simply not
> cached rather than replicating `UsageMeter`'s estimate-when-missing fallback
> on this best-effort accelerator path. Phases 1 (Redis), 2 (semantic tier),
> and 3 (console observability) remain.

### Phase 1 — Redis-backed shared store (1 slice)

- Redis adapter mirroring `build_rate_limiter` (`rate_limiter.py:108`), selected
  by `Settings.redis_url` (`config.py:311`); TTL via `EXPIRE`, LRU via
  `maxmemory-policy`. In-memory stays the fallback.
- Synthetic-stream replay: a `stream: true` request with a stored body replays it
  as chunks through the `open_chat_stream` shape, settling the tail at $0.
- **Done when:** two replicas share hits through Redis; a streamed request served
  from cache produces a well-formed SSE stream billed at $0; a Redis outage falls
  through to the provider (failure-policy regression, design §8).

> **Done — 26 July 2026.** Shipped as-designed:
> `infrastructure/cache/redis.py`'s `RedisResponseCache`, mirroring
> `RedisRateLimiter`/`RedisCircuitBreaker` byte-for-byte in shape — the client
> is typed as `object` with `redis.asyncio.Redis` imported only inside
> `build_response_cache`, so `redis` need not be installed unless
> `Settings.redis_url` selects this adapter. Selection is a straight branch on
> `settings.redis_url` in `build_response_cache`, identical to
> `build_rate_limiter`/`build_circuit_breaker`. TTL is enforced via Redis
> `SET ... EX` (one round-trip, equivalent to `SETEX`/`EXPIRE` folded into the
> write) rather than a second `EXPIRE` call; size bounding is left entirely to
> `maxmemory-policy allkeys-lru` (documented here, not enforced in code) since
> Redis is already the shared, bounded store and an app-level cap on top of it
> would just fight that policy — the in-memory adapter keeps its own
> `OrderedDict` LRU bound as the single-replica fallback's substitute for that
> guarantee. The tenant namespace is the literal Redis key
> (`CacheKey.redis_key()` → `cache:{team_id}:{api_key_id}:{digest}`), not just
> an in-process dataclass field, so isolation holds at the storage layer too.
>
> Synthetic-stream replay lives in `CompletionService.open_chat_stream`: it
> runs the *same* `_cache_key_for` participation decision `chat_completion`
> uses (global switch + team/model opt-in + request shape, `stream` excluded
> from the key as designed), and on a hit calls a new
> `_synthetic_chat_chunks` formatter that re-chunks the cached body's message
> content into the `chat.completion.chunk` wire shape (role-delta, fixed-size
> content-delta pieces, a closing delta carrying the original
> `finish_reason`, and a trailing usage-only chunk) — the same shape every
> `astream_chat_completion` adapter emits today. The tail settles through the
> *existing* `settle_cache_hit` UsageMeter path at the stored token counts and
> `cost=0.0`, reusing `_metered`'s release-once + `weakref.finalize` pattern
> so a client that drops before the first byte still releases the budget
> reservation without a spurious billing event. Any cache exception (Redis
> outage, malformed stored JSON) is caught by the pre-existing `_cache_get`
> try/except and treated as a miss, falling through to a normal provider
> stream — no new failure-policy code was needed on the streaming path,
> since it reuses the guard `_dispatch` already has for the non-streamed
> path. One deliberate simplification versus the letter of the design:
> synthetic-stream replay covers `chat.completions` (`open_chat_stream`)
> only, matching the design doc's specific wording ("the wire shape
> `open_chat_stream` emits") — `open_responses_stream` stays uncached in this
> phase, and the failover-retry streaming path is excluded from cache
> participation for the same reason Phase 0 excluded the non-streamed
> failover retry chain. Phases 2 (semantic tier) and 3 (console
> observability) remain.

### Phase 2 — Semantic tier (1 slice)

- Second tier behind the port: embed the request text via the S3 `EmbedFn`
  (`embeddings.py:30`), cosine (`embeddings.py:42`) against recent cached entries
  in the caller's own tenant scope, hit above `RESPONSE_CACHE_SEMANTIC_THRESHOLD`
  (default 0.97). Per-team opt-in only; exact-match is always tried first.
- **Done when:** with semantic opted in, a near-duplicate prompt (≥ threshold)
  hits; a below-threshold prompt misses; entries never match across tenants.

### Phase 3 — Console observability (1 slice)

- Endpoint mirroring `web/routing/controller.py:413`: hit rate + Σ(avoided cost)
  per team/model/period (`usage:read` RBAC); `cache_hit` surfaced in usage/trace
  views and, where a router decided, in the decision log.
- **Done when:** an admin sees hit rate and cost saved, and can toggle the cache
  per team/model from the console (Plan 03).

## TDD test strategy

- **Unit — key derivation:** table-driven equivalence/difference cases — same
  body ⇒ same key; changed `temperature`/`seed`/`tools`/`response_format`/
  `max_tokens` ⇒ different key; `stream` and `user` ⇒ no effect; alias vs resolved
  canonical model ⇒ same key.
- **Unit — tenant isolation:** identical body under a different `team_id` or
  `api_key_id` MISSES (design §3, the hard invariant) — this test blocks merge.
- **Integration — hit skips provider:** through the completion endpoint with a
  fake gateway asserting the provider is called once, then zero on the repeat,
  and a `cache_hit` usage event recorded at `cost=0.0` with correct token counts.
- **Regression — failure falls through:** a cache adapter whose `get`/`put` raises
  must yield a normal provider-served response (design §8) — the cache is never a
  dependency of the money path.
- **Regression — off is inert:** flag off ⇒ no lookup, no write, byte-identical.

## Risks & mitigations

- **Tenant leakage** (CRITICAL) → key is prefixed per team + api-key scope,
  derived server-side from the principal, never client-supplied; the isolation
  unit test is a merge blocker (design §3).
- **Stale / wrong responses** → determinism params are all in the key; default
  refuses `temperature > 0`; TTL bounds staleness; semantic is opt-in at a high
  0.97 threshold with exact-match always tried first.
- **Streaming correctness** → cache non-streamed only in Phase 0; replay stored
  bodies as synthetic streams in Phase 1; never store partial/aborted streams.
- **Cache latency regression** → hard time budget on `get`/`put`; a stalled Redis
  is a miss, not a stall (design §8).
- **Double billing on a hit** → the hit path overrides `cost=0.0` rather than
  routing through `_parse_usage` (`usage_meter.py:147`); asserted in the
  integration test.

## Execution

- **One branch per phase slice**, TDD (RED→GREEN), per `plans/README.md`
  conventions. Phases are sequential: Phase 1 needs Phase 0's port + integration;
  Phase 2 needs Phase 1's store; Phase 3 needs decisions to observe.
- Hexagonal boundary is law: the port lives in `domain/`, the two tiers in
  `infrastructure/cache/`, the wiring in `completion_service` + `usage_meter`.
- Gate before every PR: `just test`, `just lint`, `just typecheck`,
  `just pre-commit`; verify the merged `main`, not just the branch.
- The `cache_hit` field on `UsageEvent`/`TraceRecord` ships with an Alembic
  migration (CONTRIBUTING.md) — group it into the Phase 0 branch to keep one head.
