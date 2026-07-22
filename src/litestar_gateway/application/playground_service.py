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
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import UUID

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


def _content(response: dict[str, Any]) -> str | None:
    try:
        return response["choices"][0]["message"]["content"]
    except KeyError, IndexError, TypeError:
        return None


class PlaygroundService:
    def __init__(
        self, models: ModelRepository, credentials: CredentialRepository, gateway: LLMGateway
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway

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
        if model is None:
            return PlaygroundResult(name, ok=False, error="unknown model")
        if not model.enabled:
            return PlaygroundResult(name, ok=False, error="model is disabled")
        if model.type is not ModelType.CHAT:
            return PlaygroundResult(
                name, ok=False, error=f"'{name}' is a {model.type} model, not chat"
            )

        request: dict[str, Any] = {"model": name, "messages": messages}
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens
        clean = sanitize_request("chat.completions", request)
        clean = clamp_output_tokens("chat.completions", clean, model.max_output_tokens)

        try:
            values = await self._credentials.get_values(model.credential_id)
            if values is None:
                return PlaygroundResult(name, ok=False, error="credential missing")
            start = perf_counter()
            response = await self._gateway.achat_completion(clean, model, values)
            latency_ms = (perf_counter() - start) * 1000
        except Exception as exc:  # per-model isolation — never sink the batch
            return PlaygroundResult(name, ok=False, error=str(exc) or exc.__class__.__name__)

        usage = response.get("usage") or {}
        prompt = usage.get("prompt_tokens")
        completion = usage.get("completion_tokens")
        return PlaygroundResult(
            model_name=name,
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
