# Implementation prompt: Response Caching

> **Status — not yet implemented (design only).** This is the fuller, standalone
> version of the optional per-router semantic cache sketched in
> [`smart-routing.md`](smart-routing.md) §8 (in-memory, per-router, threshold
> 0.97, reusing the S3 embedding path). That sketch stays valid as the *routing*
> shortcut; this doc supersedes it as the **gateway-wide** feature: exact-match
> first, semantic as an opt-in tier, Redis-backed for multi-replica, tenant-scoped
> as a hard invariant, and metered through the existing money core. Where both
> exist they share the same store and the same `EmbedFn`
> (`application/routing/embeddings.py:30`); the router flag becomes a thin caller
> of this service rather than a parallel cache.

You are implementing **response caching** for this LLM gateway (Litestar,
hexagonal architecture, `src/litestar_gateway/`). A cache returns a stored
provider response for an *equivalent* later request, cutting cost and latency.
It is **off by default** and **opt-in per team and per model** — the #1 cost
saver, but never a silent correctness change.

Read `CLAUDE.md`, `CONTRIBUTING.md`, and `domain/ports/llm_gateway.py` first;
follow the conventions: Protocol ports in `domain/`, services in `application/`,
adapters in `infrastructure/`, DI everywhere, full typing, frozen dataclasses,
many small files, TDD.

---

## Core design (non-negotiable)

### 1. One port, two tiers

Define a domain port `ResponseCache` (Protocol) with the surface the request
pipeline needs — no storage detail leaks into `application/`:

```python
class ResponseCache(Protocol):
    async def get(self, key: CacheKey) -> CachedResponse | None: ...
    async def put(self, key: CacheKey, value: CachedResponse, ttl_s: int) -> None: ...
```

`CacheKey` and `CachedResponse` are frozen dataclasses. Two tiers sit behind the
port, tried in order: **exact-match** (deterministic key equality) then, only if
the team+model opted into it, **semantic** (embedding similarity ≥ threshold).
The semantic tier reuses the S3 `EmbedFn` and cosine helper verbatim
(`application/routing/embeddings.py:30`, `:42`); no second embedding path.

### 2. Cache key derivation — what makes two requests "equivalent"

The key is a hash over a **canonicalized** view, never the raw body:

- **Canonical model name** — the resolved `Model.name` after alias/router
  resolution (`_prepare`, `completion_service.py:497`), never the requested
  alias, so `gpt-4o` and an alias pointing at it share a hit.
- **Messages / input** — normalized: JSON with sorted object keys, whitespace
  preserved (semantically significant), multimodal blocks kept structurally.
- **Determinism-affecting params** — `temperature`, `top_p`, `seed`,
  `max_tokens`/`max_completion_tokens`/`max_output_tokens`, `stop`, `tools`,
  `tool_choice`, `response_format`. All part of the key: a different
  `response_format` or tool set is a different request.
- **`stream`** is NOT part of the key (see §5) — a cached non-streamed body can
  replay as a synthetic stream.
- **Ignored** — `user`, request id, and other non-determinism-affecting metadata.

Derivation is a pure function with its own unit tests; it is the single most
important thing to get right and the easiest to regress.

### 3. Tenant isolation (HARD security invariant)

**A cache entry must never be readable by a team or api-key scope other than the
one that wrote it.** The stored key is prefixed with a **per-team namespace and
per-api-key scope** (`cache:{team_id}:{api_key_id}:{hash}`), and the lookup only
ever queries within the caller's own prefix. Cross-tenant reuse is a data-leak,
not an optimization — there is no config flag that relaxes this. The namespace is
derived server-side from the authenticated principal (`web/principal.py`), never
from anything client-supplied. Unit tests assert that an identical body under a
different `team_id` or `api_key_id` misses.

### 4. Storage

- **Redis-backed for multi-replica** (mirror `build_rate_limiter`,
  `infrastructure/rate_limiter.py:108`, driven by `Settings.redis_url`,
  `config.py:311`); in-memory `OrderedDict` LRU fallback when `REDIS_URL` is
  unset, exactly as rate limiting and the S3 route cache already degrade.
- **TTL** per entry (default e.g. 3600 s, configurable) via Redis `EXPIRE`.
- **Bounded size / LRU** — Redis `maxmemory-policy allkeys-lru` or an app-level
  bound; the in-memory fallback uses the `OrderedDict` eviction pattern from
  `embeddings.py:126`.
- Values store the full provider response body plus its authoritative token
  usage (so a hit can be metered at the *real* token counts, §6).

### 5. Streaming

Cache **non-streamed responses only** initially. A streamed request
(`stream: true`) with a stored entry **replays the stored full body as a
synthetic stream** — re-chunk the cached completion into the wire shape the
`open_chat_stream` path emits, then run the tail settlement at $0. A streamed
request whose body is not yet cached is passed through uncached in Phase 0–1
(caching live SSE tails is Phase 2+ if it proves worthwhile). Never store a
partial/aborted stream.

### 6. Metering & attribution of a hit (never free of record)

A cache hit still flows through `UsageMeter` so usage stays attributable, but at
**zero provider cost**. Reuse `settle_ok` (`usage_meter.py:313`) with the stored
token counts and a `cost` override of `0.0` (add a `cache_hit` path rather than
recomputing via `_parse_usage`, `usage_meter.py:147`, which would bill the
tokens). The `UsageEvent`/`TraceRecord` gain a `cache_hit=True` marker; the
budget reservation taken at `admit` (`usage_meter.py:239`) is released as today
by `_dispatch`'s finally (`completion_service.py:178`). Where a routing decision
was made, mark it `cache_hit` in the decision log too (reconciling with
`smart-routing.md` §8).

### 7. Correctness rules (when NOT to cache)

- **Never cache errors** — only a successful, fully-formed provider response is
  written (the write sits after `settle_ok`, `completion_service.py:165`).
- **Never cache when `temperature > 0`** unless the team explicitly opts into
  "cache non-deterministic" — sampled outputs are not reusable by default.
- **Respect `max_tokens`** and all §2 params — they are in the key, so a request
  asking for more tokens cannot be served a shorter cached body.
- **Never cache** requests carrying obviously per-call state the key can't
  capture (e.g. `store`/stateful Responses threads) — bypass and pass through.

### 8. Failure policy (a cache error must NEVER fail the request)

Any cache exception, timeout, or malformed entry → **log and fall through to the
provider**, exactly as the routing failure policy falls back to `default_model`
(`smart-routing.md` §4). A `get` that raises is a miss; a `put` that raises is
swallowed. The cache is a best-effort accelerator on the side of the money path,
never a dependency of it. Give `get`/`put` a small hard time budget so a stalled
Redis cannot add latency to the request it was meant to speed up.

### 9. Integration point

Single insertion, mirroring how smart routing hooks into one place. The model is
resolved and admitted in `_prepare` (`completion_service.py:497`); the provider
call is `response = await call()` inside `_dispatch`
(`completion_service.py:158`).

- **Lookup** goes immediately *before* `await call()` (`completion_service.py:158`):
  on a hit, skip the provider entirely, settle at $0 (§6), and return the stored
  body. Only `chat.completions` / `responses` non-streamed participate initially
  (gate by `operation`); embeddings/images/native bypass.
- **Write** goes immediately *after* `settle_ok` and before `return response`
  (`completion_service.py:165`–`:176`), so only successful responses are stored.

The pipeline (clamping, admission, metering) is otherwise untouched — the cache
only short-circuits the provider round-trip.

### 10. Observability & config

- **Headline metric: cost saved** — mirror the routing "savings" endpoint
  (`web/routing/controller.py:413`). Expose hit rate and Σ(avoided cost) per
  team/model/period; avoided cost = what the request *would* have cost at the
  model's unit price × the stored token counts.
- **Config surface**: `RESPONSE_CACHE_ENABLED` (global kill-switch, default off),
  `RESPONSE_CACHE_TTL_S`, `RESPONSE_CACHE_MAX_ENTRIES`,
  `RESPONSE_CACHE_SEMANTIC_THRESHOLD` (default 0.97, matching §8) in `Settings`
  (`config.py:281` `from_env`), plus a **per-team / per-model toggle** persisted
  as a flag (exact-match on/off, semantic on/off, allow-non-deterministic on/off)
  and surfaced in the admin console (Plan 03) as read-then-edit.

---

## Explicit non-goals (do not build)

- Caching streamed SSE *tails* live (Phase 2+ only if measured worthwhile).
- Cross-tenant / global shared cache — forbidden by §3, not a future option.
- Semantic cache without an exact-match tier in front of it.
- Invalidation-by-content-change, cache warming, or write-through prefetch.
- A external vector DB for the semantic tier — the bounded store matches §8.

## Delivery

See the execution plan: [`plans/04-response-caching.md`](../../plans/04-response-caching.md).
