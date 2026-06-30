"""Application service for team-scoped model deployments.

Authorization (platform admin or team admin) is enforced by the caller via
`TeamService.ensure_can_manage_team`; this service owns the model invariants:
unique name per team and provider == referenced credential's provider.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from litestar_test.domain.entities import Model, ModelType, Provider
from litestar_test.domain.exceptions import (
    CredentialNotFound,
    ModelNameExists,
    ModelNotFound,
    ProviderMismatch,
)
from litestar_test.domain.ports import CredentialRepository, ModelRepository


def _now() -> datetime:
    return datetime.now(UTC)


class ModelService:
    def __init__(self, models: ModelRepository, credentials: CredentialRepository) -> None:
        self._models = models
        self._credentials = credentials

    async def _validate_credential(self, provider: Provider, credential_id: UUID) -> None:
        credential = await self._credentials.get(credential_id)
        if credential is None:
            raise CredentialNotFound(str(credential_id))
        if credential.provider != provider:
            raise ProviderMismatch(
                f"Model provider '{provider}' does not match credential provider "
                f"'{credential.provider}'"
            )

    async def create(
        self,
        team_id: UUID,
        name: str,
        provider: Provider,
        credential_id: UUID,
        model_type: ModelType,
        provider_model_id: str,
        params: dict[str, Any] | None = None,
        api_base: str | None = None,
        api_version: str | None = None,
        input_cost_per_token: float | None = None,
        output_cost_per_token: float | None = None,
        enabled: bool = True,
    ) -> Model:
        if await self._models.get_by_name(team_id, name) is not None:
            raise ModelNameExists(name)
        await self._validate_credential(provider, credential_id)
        return await self._models.add(
            Model(
                id=uuid4(),
                team_id=team_id,
                name=name,
                provider=provider,
                credential_id=credential_id,
                type=model_type,
                provider_model_id=provider_model_id,
                params=params or {},
                api_base=api_base,
                api_version=api_version,
                input_cost_per_token=input_cost_per_token,
                output_cost_per_token=output_cost_per_token,
                enabled=enabled,
                created_at=_now(),
            )
        )

    async def list_for_team(self, team_id: UUID) -> list[Model]:
        return await self._models.list_by_team(team_id)

    async def _get_scoped(self, team_id: UUID, model_id: UUID) -> Model:
        model = await self._models.get(model_id)
        if model is None or model.team_id != team_id:
            raise ModelNotFound(str(model_id))
        return model

    async def update(self, team_id: UUID, model_id: UUID, **changes: Any) -> Model:
        """Apply the given non-None field changes. `provider`/`credential_id`
        are immutable here; recreate the model to change the provider."""
        model = await self._get_scoped(team_id, model_id)
        applied = {k: v for k, v in changes.items() if v is not None}
        return await self._models.update(dataclasses.replace(model, **applied))

    async def delete(self, team_id: UUID, model_id: UUID) -> None:
        await self._get_scoped(team_id, model_id)
        await self._models.remove(model_id)
