"""Playground: run one prompt against several models and compare the results.

Real provider calls (so latency and outputs are truthful) delegated to the
normal completion path.  They therefore share its request policy, budget,
rate-limit, usage-ledger, and trace guarantees.  The batch is bounded and
deduplicated before any provider work starts.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any
from uuid import UUID

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.entities import Model, ModelType
from litestar_gateway.domain.exceptions import (
    BudgetExceeded,
    InvalidPlaygroundRequest,
    RateLimited,
)
from litestar_gateway.domain.ports import ModelRepository

DEFAULT_MAX_MODELS = 5
DEFAULT_MAX_CONCURRENCY = 1


@dataclass(frozen=True)
class PlaygroundResult:
    model_name: str
    ok: bool
    content: str | None = None
    latency_ms: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost: float | None = None
    error: str | None = None
    # For a router: the candidate it selected for this request.
    chosen_model: str | None = None


def _content(response: dict[str, Any]) -> str | None:
    try:
        return response["choices"][0]["message"]["content"]
    except KeyError, IndexError, TypeError:
        return None


class PlaygroundService:
    def __init__(
        self,
        models: ModelRepository,
        completion_service: CompletionService,
        routers: RouterService | None = None,
        *,
        max_models: int = DEFAULT_MAX_MODELS,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ) -> None:
        self._models = models
        self._completion_service = completion_service
        self._routers = routers
        self._max_models = max_models
        self._max_concurrency = max_concurrency

    async def compare(
        self,
        team_id: UUID,
        model_names: list[str],
        messages: list[dict[str, Any]],
        max_completion_tokens: int | None = None,
    ) -> list[PlaygroundResult]:
        """Run one governed call per unique alias, preserving request order."""
        if not model_names:
            raise InvalidPlaygroundRequest("select at least one model")
        if len(model_names) > self._max_models:
            raise InvalidPlaygroundRequest(f"select at most {self._max_models} models")
        if any(not name or len(name) > 200 for name in model_names):
            raise InvalidPlaygroundRequest("model aliases must contain 1 to 200 characters")
        if not messages or len(messages) > 100:
            raise InvalidPlaygroundRequest("send between 1 and 100 messages")
        if max_completion_tokens is not None and (
            isinstance(max_completion_tokens, bool)
            or max_completion_tokens < 1
            or max_completion_tokens > 1_000_000
        ):
            raise InvalidPlaygroundRequest("max_completion_tokens must be between 1 and 1000000")
        unique_names = list(dict.fromkeys(model_names))
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run_bounded(name: str) -> PlaygroundResult:
            async with semaphore:
                return await self._run_one(team_id, name, messages, max_completion_tokens)

        results = await asyncio.gather(*(run_bounded(name) for name in unique_names))
        return list(results)

    async def _run_one(
        self,
        team_id: UUID,
        name: str,
        messages: list[dict[str, Any]],
        max_completion_tokens: int | None,
    ) -> PlaygroundResult:
        model = await self._models.get_by_name(team_id, name)
        if model is not None:
            if not model.enabled:
                return PlaygroundResult(name, ok=False, error="model is disabled")
            if model.type is not ModelType.CHAT:
                return PlaygroundResult(
                    name, ok=False, error=f"'{name}' is a {model.type} model, not chat"
                )
            return await self._call_model(team_id, name, model, messages, max_completion_tokens)

        # Not a model — maybe a router. Preview which candidate it picks, then
        # call that candidate; label the result with the router + its choice.
        if self._routers is not None:
            router = await self._routers.get_enabled_by_name(team_id, name)
            if router is not None:
                try:
                    request = {"model": name, "messages": messages}
                    decision = await self._routers.select_preview(
                        router, request, acting_team_id=team_id
                    )
                except Exception as exc:
                    return PlaygroundResult(name, ok=False, error=str(exc) or "routing failed")
                chosen = await self._models.get_by_name(team_id, decision.model_name)
                if chosen is None:
                    return PlaygroundResult(
                        name, ok=False, error=f"router chose unknown model '{decision.model_name}'"
                    )
                result = await self._call_model(
                    team_id,
                    name,
                    chosen,
                    messages,
                    max_completion_tokens,
                    call_alias=decision.model_name,
                )
                return replace(result, chosen_model=decision.model_name)

        return PlaygroundResult(name, ok=False, error="unknown model")

    async def _call_model(
        self,
        team_id: UUID,
        label: str,
        model: Model,
        messages: list[dict[str, Any]],
        max_completion_tokens: int | None,
        *,
        call_alias: str | None = None,
    ) -> PlaygroundResult:
        request: dict[str, Any] = {"model": call_alias or label, "messages": messages}
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens
        try:
            start = perf_counter()
            response = await self._completion_service.chat_completion(team_id, None, request)
            latency_ms = (perf_counter() - start) * 1000
        except BudgetExceeded, RateLimited:
            # Governance failures apply to the whole HTTP request and retain
            # their normal 402/429 mapping instead of being hidden in a 201 row.
            raise
        except Exception as exc:  # per-model isolation — never sink the batch
            return PlaygroundResult(label, ok=False, error=str(exc) or exc.__class__.__name__)

        usage = response.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        return PlaygroundResult(
            model_name=label,
            ok=True,
            content=_content(response),
            latency_ms=latency_ms,
            prompt_tokens=prompt if isinstance(prompt, int) else None,
            completion_tokens=completion if isinstance(completion, int) else None,
            cost=_cost(model, prompt, completion),
        )


def _cost(model: Model, prompt: object, completion: object) -> float | None:
    """Estimated cost from the model's per-token prices, or None when either the
    prices or the token counts are unavailable."""
    if model.input_cost_per_token is None or model.output_cost_per_token is None:
        return None
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return prompt * model.input_cost_per_token + completion * model.output_cost_per_token
