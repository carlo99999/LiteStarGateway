# Semantic routing with embeddings

Semantic routing is the smart-routing strategy used when a router has
`strategy: "embeddings"`. It maps user text to one of the router candidates by
comparing the request embedding with example utterances configured by the admin.

## Runtime flow

1. The admin configures routes with:
   - `name`
   - `target_model`
   - `utterances`
   - optional `threshold` (`0.80` by default)
2. `RouterService` validates that `embedding_model` is an enabled embeddings
   model for the team.
3. `RouterService` validates that every route target is one of the router's
   declared candidates.
4. At request time, hard capability filters run first. The embeddings strategy
   only chooses among surviving candidates.
5. The strategy embeds the request text through the gateway's existing
   `LLMGateway` port, using the team's configured embedding model and
   credentials.
6. The strategy compares the request vector with every example utterance vector
   for each route using cosine similarity.
7. The best route whose score is at or above its threshold wins.
8. If no route crosses its threshold, the router's `default_model` is chosen.
9. If the strategy fails or times out, `RouterService` falls back according to
   the normal smart-routing never-fail policy.

## Cache behavior

The current implementation intentionally uses an in-process bounded cache, not a
vector database.

- Route utterance embeddings are computed lazily on first use.
- The cache key is a hash of the embedding model and route config, so editing a
  router naturally creates a new cache entry.
- The cache is LRU-bounded by `MAX_CACHE_ENTRIES` (`32` route configurations).
- There is no TTL. Entries remain until the process restarts, the cache evicts
  them, or the router config changes.
- Locks are evicted with their cache entries, so normal router/config churn does
  not create an unbounded lock leak.
- The cache is process-local. Multiple app workers each keep their own cache and
  pay their own cold-start embedding cost.

This is acceptable for the current expected scale: a moderate number of routers,
routes, and example utterances. It avoids adding a new infrastructure dependency
for a routing feature that normally stores a small reference set.

## Vector DB decision

A vector database is deliberately deferred. Use the in-memory cache until one of
these conditions is true:

- route example sets become large enough that one cached config can consume
  meaningful memory;
- cold-start embedding cost after deploys or restarts becomes operationally
  visible;
- multiple workers need a shared precomputed route index;
- admins need searchable/debuggable route-match records outside process memory;
- top-k route search evolves beyond the current small reference-set scan.

When that happens, prefer adding a `SemanticRouteIndex` port instead of changing
the strategy semantics. The strategy should continue to embed the request text,
ask the index for route matches, enforce thresholds, and fall back to
`default_model` exactly as it does today.

The first production backend should likely be Postgres with `pgvector`, because
the project already has relational persistence, migrations, team-scoped data,
router CRUD, and decision observability. Dedicated vector services such as
Qdrant, Milvus, or Pinecone should be considered only if pgvector becomes a real
bottleneck.

## Future shape

A future indexed implementation can keep the current behavior behind a port:

```python
class SemanticRouteIndex(Protocol):
    async def upsert_routes(
        self,
        router_id: UUID,
        embedding_model: str,
        routes: tuple[SemanticRoute, ...],
    ) -> None: ...

    async def search(
        self,
        router_id: UUID,
        query_vector: list[float],
        limit: int,
    ) -> list[SemanticRouteMatch]: ...

    async def delete_router(self, router_id: UUID) -> None: ...
```

Keep the in-memory backend as the default compatibility path and add an indexed
backend as an explicit deployment choice when scale requires it.
