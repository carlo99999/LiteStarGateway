"""Orchestrates an OpenAI-compatible call for a team.

Resolves the request's `model` alias to the team's `Model`, checks it is enabled,
decrypts the referenced credential, and dispatches to the `LLMGateway`. This path
is async (it touches the DB); the sync gateway methods are for library use where
the caller already holds the model and credentials.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from litestar_test.domain.entities import Model, ModelType, UsageEvent
from litestar_test.domain.exceptions import (
    CredentialNotFound,
    ModelDisabled,
    ModelNotFound,
    ModelTypeMismatch,
)
from litestar_test.domain.ports import (
    CredentialRepository,
    LLMGateway,
    ModelRepository,
    UsageRepository,
)
from litestar_test.domain.request_policy import sanitize_request

logger = logging.getLogger("litestar_test.usage")


class CompletionService:
    def __init__(
        self,
        models: ModelRepository,
        credentials: CredentialRepository,
        gateway: LLMGateway,
        usage: UsageRepository,
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._usage = usage

    async def _record_usage(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        response: dict[str, Any],
    ) -> None:
        """Record token usage + estimated cost. Fail-safe: never breaks the call."""
        usage = response.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        cost = prompt * (model.input_cost_per_token or 0.0) + completion * (
            model.output_cost_per_token or 0.0
        )
        try:
            await self._usage.record(
                UsageEvent(
                    id=uuid4(),
                    team_id=team_id,
                    api_key_id=api_key_id,
                    model_id=model.id,
                    model_name=model.name,
                    operation=operation,
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    cost=cost,
                    created_at=datetime.now(UTC),
                )
            )
        except Exception:  # pragma: no cover - recording must not fail the request
            logger.warning("failed to record usage", exc_info=True)

    async def _prepare(
        self, team_id: UUID, request: dict[str, Any], expected_type: ModelType
    ) -> tuple[Model, dict[str, str]]:
        alias = request.get("model")
        model = await self._models.get_by_name(team_id, alias) if alias else None
        if model is None:
            raise ModelNotFound(str(alias))
        if not model.enabled:
            raise ModelDisabled(model.name)
        if model.type != expected_type:
            raise ModelTypeMismatch(
                f"Model '{model.name}' is type '{model.type}', not '{expected_type}'"
            )
        values = await self._credentials.get_values(model.credential_id)
        if values is None:
            raise CredentialNotFound(str(model.credential_id))
        return model, values

    async def chat_completion(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("chat.completions", request)
        response = await self._gateway.achat_completion(clean, model, values)
        await self._record_usage(team_id, api_key_id, model, "chat.completions", response)
        return response

    async def responses(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("responses", request)
        response = await self._gateway.aresponses(clean, model, values)
        await self._record_usage(team_id, api_key_id, model, "responses", response)
        return response

    async def open_chat_stream(
        self, team_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts. Awaited before streaming starts."""
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("chat.completions", request)
        return await self._gateway.astream_chat_completion(clean, model, values)

    async def open_responses_stream(
        self, team_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve (may raise → HTTP error) and return an async iterator of
        Responses-API stream events. Awaited before streaming starts."""
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("responses", request)
        return await self._gateway.astream_responses(clean, model, values)

    async def embeddings(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.EMBEDDINGS)
        clean = sanitize_request("embeddings", request)
        response = await self._gateway.aembeddings(clean, model, values)
        await self._record_usage(team_id, api_key_id, model, "embeddings", response)
        return response

    async def images(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.IMAGE)
        clean = sanitize_request("images", request)
        response = await self._gateway.aimages(clean, model, values)
        await self._record_usage(team_id, api_key_id, model, "images", response)
        return response
