"""The `complete` seam a judge guardrail calls, billed to the calling team.

A judge is a real provider call made inside someone else's request, so it is
billed and traced like any other — under its own operation name
(`guardrail.judge`), never folded into the model the caller actually asked for.
An operator looking at a bill must be able to see what the guardrail cost them;
hiding it inside the caller's line item would make the safety layer look free.

Mirrors `RouterService._judge_complete` in shape rather than reusing it: that one
bills as `routing.judge` and resolves through a router's config, and a shared
helper that had to serve both would take a mode flag for no gain.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any
from uuid import UUID

from litestar_gateway.application.guardrails.judge import CompleteFn
from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import ModelType
from litestar_gateway.domain.exceptions import ModelNotFound, ModelTypeMismatch
from litestar_gateway.domain.ports import CredentialRepository, LLMGateway, ModelRepository

logger = logging.getLogger("litestar_gateway.guardrails")

OPERATION = "guardrail.judge"


def judge_completer(
    *,
    models: ModelRepository,
    credentials: CredentialRepository,
    gateway: LLMGateway,
    meter: UsageMeter | None = None,
    team_id: UUID,
    api_key_id: UUID | None = None,
) -> CompleteFn:
    """A `CompleteFn` that resolves the judge model in `team_id` and calls it."""

    async def complete(model_name: str, request: dict[str, Any]) -> dict[str, Any]:
        model = await models.get_by_name(team_id, model_name)
        if model is None or not model.enabled:
            # Raised, not swallowed: an unresolvable judge is a provider failure,
            # and the rule's fail policy — not this function — decides whether
            # that means "allow" or "refuse".
            raise ModelNotFound(f"guardrail judge model '{model_name}' is not available")
        if model.type is not ModelType.CHAT:
            raise ModelTypeMismatch(f"guardrail judge model '{model_name}' is not a chat model")
        values = await credentials.get_values(model.credential_id)
        if values is None:
            raise ModelNotFound(f"credential missing for guardrail judge model '{model_name}'")
        if meter is None or api_key_id is None:
            return await gateway.achat_completion(request, model, values)
        reservation = await meter.admit(team_id, model, request)
        start = perf_counter()
        try:
            try:
                response = await gateway.achat_completion(request, model, values)
            except Exception as exc:
                meter.trace_error(
                    team_id, api_key_id, model, OPERATION, (perf_counter() - start) * 1000, exc
                )
                raise
            await meter.settle_ok(
                team_id,
                api_key_id,
                model,
                OPERATION,
                response,
                (perf_counter() - start) * 1000,
                request,
            )
            return response
        finally:
            await meter.release(reservation)

    return complete
