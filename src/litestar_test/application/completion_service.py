"""Orchestrates an OpenAI-compatible call for a team.

Resolves the request's `model` alias to the team's `Model`, checks it is enabled,
decrypts the referenced credential, and dispatches to the `LLMGateway`. This path
is async (it touches the DB); the sync gateway methods are for library use where
the caller already holds the model and credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from litestar_test.domain.entities import Model, ModelType
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
)


class CompletionService:
    def __init__(
        self,
        models: ModelRepository,
        credentials: CredentialRepository,
        gateway: LLMGateway,
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._gateway = gateway

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

    async def chat_completion(self, team_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        return await self._gateway.achat_completion(request, model, values)

    async def responses(self, team_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        return await self._gateway.aresponses(request, model, values)

    async def open_chat_stream(
        self, team_id: UUID, request: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Resolve the model + credentials (may raise → HTTP error) and return an
        async iterator of OpenAI chunk dicts. Awaited before streaming starts."""
        model, values = await self._prepare(team_id, request, ModelType.CHAT)
        return await self._gateway.astream_chat_completion(request, model, values)

    async def embeddings(self, team_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.EMBEDDINGS)
        return await self._gateway.aembeddings(request, model, values)

    async def images(self, team_id: UUID, request: dict[str, Any]) -> dict[str, Any]:
        model, values = await self._prepare(team_id, request, ModelType.IMAGE)
        return await self._gateway.aimages(request, model, values)
