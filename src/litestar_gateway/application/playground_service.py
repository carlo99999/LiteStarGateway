"""Playground: run one prompt against several models and compare the results.

Real provider calls (so latency and outputs are truthful), governed by the same
request sanitizing/clamping as the inference path — but deliberately *not*
metered or billed: it's an admin comparison tool, not customer traffic, and
shouldn't drain a team's budget. Cost is computed from each model's per-token
costs for display only. Each model runs concurrently and isolates its own
error, so one failure never sinks the comparison.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any
from uuid import UUID

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.entities import Model, ModelType
from litestar_gateway.domain.ports import CredentialRepository, LLMGateway, ModelRepository
from litestar_gateway.domain.request_policy import clamp_output_tokens, sanitize_request


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
        credentials: CredentialRepository,
        gateway: LLMGateway,
        routers: RouterService | None = None,
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._routers = routers

    async def compare(
        self,
        team_id: UUID,
        model_names: list[str],
        messages: list[dict[str, Any]],
        max_completion_tokens: int | None = None,
    ) -> list[PlaygroundResult]:
        """Send the same messages to each model concurrently; return one result
        each, in the requested order."""
        results = await asyncio.gather(
            *(self._run_one(team_id, name, messages, max_completion_tokens) for name in model_names)
        )
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
                    team_id, name, chosen, messages, max_completion_tokens
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
    ) -> PlaygroundResult:
        request: dict[str, Any] = {"model": model.name, "messages": messages}
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens
        clean = sanitize_request("chat.completions", request)
        clean = clamp_output_tokens("chat.completions", clean, model.max_output_tokens)

        try:
            values = await self._credentials.get_values(model.credential_id)
            if values is None:
                return PlaygroundResult(label, ok=False, error="credential missing")
            start = perf_counter()
            response = await self._gateway.achat_completion(clean, model, values)
            latency_ms = (perf_counter() - start) * 1000
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
