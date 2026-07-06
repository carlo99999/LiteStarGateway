"""Orchestrates an OpenAI-compatible call for a team.

Resolves the request's `model` alias to the team's `Model`, checks it is enabled,
decrypts the referenced credential, and dispatches to the `LLMGateway`. This path
is async (it touches the DB); the sync gateway methods are for library use where
the caller already holds the model and credentials. Everything money-side —
budget admission, usage metering, billing, traces — is delegated to `UsageMeter`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from time import perf_counter
from typing import Any
from uuid import UUID

from litestar_gateway.application.usage_meter import UsageMeter
from litestar_gateway.domain.entities import Model, ModelType
from litestar_gateway.domain.exceptions import (
    CredentialNotFound,
    ModelDisabled,
    ModelNotFound,
    ModelTypeMismatch,
)
from litestar_gateway.domain.ports import (
    CredentialRepository,
    LLMGateway,
    ModelRepository,
)
from litestar_gateway.domain.request_policy import clamp_output_tokens, sanitize_request


class CompletionService:
    def __init__(
        self,
        models: ModelRepository,
        credentials: CredentialRepository,
        gateway: LLMGateway,
        meter: UsageMeter,
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway
        self._meter = meter

    async def _dispatch(
        self,
        team_id: UUID,
        api_key_id: UUID,
        model: Model,
        operation: str,
        request: dict[str, Any],
        call: Callable[[], Awaitable[dict[str, Any]]],
        reservation: float = 0.0,
    ) -> dict[str, Any]:
        """Run one gateway call, observing success (usage + trace) and failure
        (error trace) before the exception propagates to the HTTP layer. The
        budget reservation taken at admission is released either way. The request
        is passed to settlement so usage can be estimated if the provider
        reported none (H14)."""
        start = perf_counter()
        try:
            try:
                response = await call()
            except Exception as exc:
                self._meter.trace_error(
                    team_id, api_key_id, model, operation, (perf_counter() - start) * 1000, exc
                )
                raise
            latency_ms = (perf_counter() - start) * 1000
            await self._meter.settle_ok(
                team_id, api_key_id, model, operation, response, latency_ms, request
            )
            return response
        finally:
            self._meter.release(team_id, reservation)

    async def _prepare(
        self, team_id: UUID, operation: str, request: dict[str, Any], expected_type: ModelType
    ) -> tuple[Model, dict[str, str], float, dict[str, Any]]:
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
        # Per-model output ceiling: clamp/inject now that the model is known, and
        # reserve from the clamped request so admission and the provider call agree.
        clean = clamp_output_tokens(operation, request, model.max_output_tokens)
        reservation = await self._meter.admit(team_id, model, clean)
        try:
            values = await self._credentials.get_values(model.credential_id)
            if values is None:
                raise CredentialNotFound(str(model.credential_id))
        except BaseException:
            self._meter.release(team_id, reservation)
            raise
        return model, values, reservation, clean

    async def chat_completion(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("chat.completions", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "chat.completions", clean, ModelType.CHAT
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "chat.completions",
            clean,
            lambda: self._gateway.achat_completion(clean, model, values),
            reservation,
        )

    async def responses(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("responses", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "responses", clean, ModelType.CHAT
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "responses",
            clean,
            lambda: self._gateway.aresponses(clean, model, values),
            reservation,
        )

    async def open_chat_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts, metered for usage. Awaited before
        streaming starts so resolution errors surface as HTTP status codes."""
        clean = sanitize_request("chat.completions", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "chat.completions", clean, ModelType.CHAT
        )
        try:
            stream = await self._gateway.astream_chat_completion(clean, model, values)
        except BaseException:
            self._meter.release(team_id, reservation)
            raise
        return self._meter.metered_stream(
            team_id, api_key_id, model, "chat.completions", stream, clean, reservation
        )

    async def open_responses_stream(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve (may raise → HTTP error) and return an async iterator of
        Responses-API stream events, metered for usage."""
        clean = sanitize_request("responses", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "responses", clean, ModelType.CHAT
        )
        try:
            stream = await self._gateway.astream_responses(clean, model, values)
        except BaseException:
            self._meter.release(team_id, reservation)
            raise
        return self._meter.metered_stream(
            team_id, api_key_id, model, "responses", stream, clean, reservation
        )

    async def embeddings(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("embeddings", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "embeddings", clean, ModelType.EMBEDDINGS
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "embeddings",
            clean,
            lambda: self._gateway.aembeddings(clean, model, values),
            reservation,
        )

    async def images(
        self, team_id: UUID, api_key_id: UUID, request: dict[str, Any]
    ) -> dict[str, Any]:
        clean = sanitize_request("images", request)
        model, values, reservation, clean = await self._prepare(
            team_id, "images", clean, ModelType.IMAGE
        )
        return await self._dispatch(
            team_id,
            api_key_id,
            model,
            "images",
            clean,
            lambda: self._gateway.aimages(clean, model, values),
            reservation,
        )
