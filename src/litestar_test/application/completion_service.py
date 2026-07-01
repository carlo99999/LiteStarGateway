"""Orchestrates an OpenAI-compatible call for a team.

Resolves the request's `model` alias to the team's `Model`, checks it is enabled,
decrypts the referenced credential, and dispatches to the `LLMGateway`. This path
is async (it touches the DB); the sync gateway methods are for library use where
the caller already holds the model and credentials.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from litestar_test.domain.entities import Model, ModelType, TraceRecord, UsageEvent
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
        emit_trace: Callable[[TraceRecord], None],
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._usage = usage
        self._emit_trace = emit_trace

    async def _record_usage(self, event: UsageEvent) -> None:
        """Persist the billing record. A failed write must never fail the request,
        but it must not vanish silently either: on failure we log the full event at
        ERROR (no secrets — ids, tokens, cost) so billing can be reconciled/replayed
        from logs rather than under-counting invisibly."""
        try:
            await self._usage.record(event)
        except Exception:  # recording must not fail the request
            logger.error(
                "usage event dropped (record failed): "
                "team=%s api_key=%s model=%s op=%s prompt=%s completion=%s cost=%s at=%s",
                event.team_id,
                event.api_key_id,
                event.model_name,
                event.operation,
                event.prompt_tokens,
                event.completion_tokens,
                event.cost,
                event.created_at.isoformat(),
                exc_info=True,
            )

    async def _observe(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        response: dict[str, Any],
        latency_ms: float,
    ) -> None:
        """Record usage (billing) + emit an observability trace. Fail-safe."""
        usage = response.get("usage") or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        cost = prompt * (model.input_cost_per_token or 0.0) + completion * (
            model.output_cost_per_token or 0.0
        )
        now = datetime.now(UTC)
        # Usage = authoritative billing record (persisted).
        event = UsageEvent(
            id=uuid4(),
            team_id=team_id,
            api_key_id=api_key_id,
            model_id=model.id,
            model_name=model.name,
            operation=operation,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost=cost,
            created_at=now,
        )
        await self._record_usage(event)
        # Trace = observability (latency/analytics), fire-and-forget off the path.
        self._emit_trace(
            TraceRecord(
                team_id=team_id,
                api_key_id=api_key_id,
                model_name=model.name,
                provider=model.provider.value,
                operation=operation,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost=cost,
                latency_ms=latency_ms,
                status="ok",
                created_at=now,
            )
        )

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
        start = perf_counter()
        response = await self._gateway.achat_completion(clean, model, values)
        latency_ms = (perf_counter() - start) * 1000
        await self._observe(team_id, api_key_id, model, "chat.completions", response, latency_ms)
        return response

    async def responses(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("responses", request)
        start = perf_counter()
        response = await self._gateway.aresponses(clean, model, values)
        latency_ms = (perf_counter() - start) * 1000
        await self._observe(team_id, api_key_id, model, "responses", response, latency_ms)
        return response

    async def _metered_stream(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        stream: AsyncIterator[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        """Relay chunks unchanged while capturing usage as it flows, then record a
        UsageEvent + emit a trace once the stream finishes (or the client
        disconnects — the `finally` runs on generator close). Without this,
        streamed calls were neither billed nor observed."""
        start = perf_counter()
        usage: dict[str, Any] = {}
        try:
            async for chunk in stream:
                # OpenAI chat puts usage at the top level (final chunk); the
                # Responses API nests it under `response`.
                found = chunk.get("usage") or (chunk.get("response") or {}).get("usage")
                if found:
                    usage = found
                yield chunk
        finally:
            latency_ms = (perf_counter() - start) * 1000
            await self._observe(team_id, api_key_id, model, operation, {"usage": usage}, latency_ms)

    async def open_chat_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts, metered for usage. Awaited before
        streaming starts so resolution errors surface as HTTP status codes."""
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("chat.completions", request)
        stream = await self._gateway.astream_chat_completion(clean, model, values)
        return self._metered_stream(team_id, api_key_id, model, "chat.completions", stream)

    async def open_responses_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve (may raise → HTTP error) and return an async iterator of
        Responses-API stream events, metered for usage."""
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        clean = sanitize_request("responses", request)
        stream = await self._gateway.astream_responses(clean, model, values)
        return self._metered_stream(team_id, api_key_id, model, "responses", stream)

    async def embeddings(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.EMBEDDINGS)
        clean = sanitize_request("embeddings", request)
        start = perf_counter()
        response = await self._gateway.aembeddings(clean, model, values)
        latency_ms = (perf_counter() - start) * 1000
        await self._observe(team_id, api_key_id, model, "embeddings", response, latency_ms)
        return response

    async def images(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.IMAGE)
        clean = sanitize_request("images", request)
        start = perf_counter()
        response = await self._gateway.aimages(clean, model, values)
        latency_ms = (perf_counter() - start) * 1000
        await self._observe(team_id, api_key_id, model, "images", response, latency_ms)
        return response
