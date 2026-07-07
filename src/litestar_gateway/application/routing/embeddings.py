"""S3 — semantic routes via embeddings.

The admin declares routes (name → target model, example utterances, similarity
threshold) in the strategy config; the user text is embedded **through the
gateway's own `LLMGateway` port** (reusing the team's embedding model and
existing provider adapters) and compared by cosine similarity against the
routes' pre-computed utterance embeddings. Best route above its threshold
wins; below every threshold the router's `default_model` is chosen. Utterance
embeddings are computed lazily at first use and cached in memory per router
config — no external vector store at this scale.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from litestar_gateway.domain.routing import CandidateModel, RoutingContext, RoutingDecision

STRATEGY_ID = "embeddings"

# async (embedding_model_name, texts) -> vectors
EmbedFn = Callable[[str, list[str]], Awaitable[list[list[float]]]]
DEFAULT_THRESHOLD = 0.80

# (cache_key) → list of (route_name, target_model, threshold, [vectors]).
# Keyed by a hash of the routes config + embedding model, so editing the router
# invalidates naturally. Bounded by the number of distinct router configs.
_ROUTE_CACHE: dict[str, list[tuple[str, str, float, list[list[float]]]]] = {}
_CACHE_LOCKS: dict[str, asyncio.Lock] = {}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


@dataclass(frozen=True)
class _Route:
    name: str
    target_model: str
    utterances: tuple[str, ...]
    threshold: float


def _parse_routes(config: dict[str, Any]) -> tuple[_Route, ...]:
    raw = config.get("routes")
    if not isinstance(raw, list) or not raw:
        raise ValueError("embeddings strategy requires a non-empty 'routes' list")
    routes = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"route must be an object: {item!r}")
        name, target = item.get("name"), item.get("target_model")
        utterances = item.get("utterances")
        if not (isinstance(name, str) and name):
            raise ValueError("route needs a 'name'")
        if not (isinstance(target, str) and target):
            raise ValueError(f"route '{name}' needs a 'target_model'")
        if not (isinstance(utterances, list) and utterances) or not all(
            isinstance(u, str) and u for u in utterances
        ):
            raise ValueError(f"route '{name}' needs a non-empty 'utterances' list of strings")
        threshold = item.get("threshold", DEFAULT_THRESHOLD)
        if not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
            raise ValueError(f"route '{name}' threshold must be in (0, 1]")
        routes.append(_Route(name, target, tuple(utterances), float(threshold)))
    return tuple(routes)


class EmbeddingsStrategy:
    """Built by the RouterService with an `embed` callable bound to the team's
    embedding model (deps are injected — see `StrategyDeps`)."""

    def __init__(
        self, config: dict[str, Any] | None = None, *, embed: EmbedFn | None = None
    ) -> None:
        config = config or {}
        model = config.get("embedding_model")
        if not (isinstance(model, str) and model):
            raise ValueError("embeddings strategy requires an 'embedding_model' (team model name)")
        self._embedding_model = model
        self._routes = _parse_routes(config)
        self._embed: EmbedFn | None = embed
        self._cache_key = hashlib.sha256(
            json.dumps(
                {
                    "model": model,
                    "routes": [
                        r.__dict__ | {"utterances": list(r.utterances)} for r in self._routes
                    ],
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()

    async def _route_vectors(
        self, embed: EmbedFn
    ) -> list[tuple[str, str, float, list[list[float]]]]:
        cached = _ROUTE_CACHE.get(self._cache_key)
        if cached is not None:
            return cached
        lock = _CACHE_LOCKS.setdefault(self._cache_key, asyncio.Lock())
        async with lock:
            cached = _ROUTE_CACHE.get(self._cache_key)
            if cached is not None:
                return cached
            entries = []
            for route in self._routes:
                vectors = await embed(self._embedding_model, list(route.utterances))
                entries.append((route.name, route.target_model, route.threshold, vectors))
            _ROUTE_CACHE[self._cache_key] = entries
            return entries

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        embed = self._embed
        if embed is None:
            raise ValueError("embeddings strategy is missing its embed dependency")
        start = perf_counter()
        names = {candidate.model_name for candidate in candidates}
        (query_vector,) = await embed(self._embedding_model, [ctx.user_text])

        best_name, best_target, best_score = None, None, -1.0
        for route_name, target, threshold, vectors in await self._route_vectors(embed):
            score = max((_cosine(query_vector, v) for v in vectors), default=-1.0)
            if score >= threshold and score > best_score and target in names:
                best_name, best_target, best_score = route_name, target, score
        if best_target is None:
            # Below every threshold: the router's default, per the design doc.
            fallback = ctx.default_model if ctx.default_model in names else None
            if fallback is None:
                raise ValueError("no semantic route matched and default_model is not capable")
            return RoutingDecision(
                model_name=fallback,
                strategy=STRATEGY_ID,
                tier=None,
                score=None,
                signals=("below all route thresholds",),
                decision_ms=(perf_counter() - start) * 1000,
            )
        return RoutingDecision(
            model_name=best_target,
            strategy=STRATEGY_ID,
            tier=None,
            score=best_score,
            signals=(f"route '{best_name}' ({best_score:.3f})",),
            decision_ms=(perf_counter() - start) * 1000,
        )
