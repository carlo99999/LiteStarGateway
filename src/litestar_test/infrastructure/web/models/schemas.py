"""DTOs for team-scoped model deployments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from litestar_test.domain.entities import Model, ModelType, Provider


@dataclass(frozen=True)
class CreateModelRequest:
    name: str
    provider: Provider
    credential_id: UUID  # must reference a credential of the same provider
    type: ModelType
    provider_model_id: str  # upstream model name, e.g. "gpt-4o"
    params: dict[str, Any] = field(default_factory=dict)
    api_version: str | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    enabled: bool = True


@dataclass(frozen=True)
class UpdateModelRequest:
    """All fields optional; `null`/omitted leaves the value unchanged. The
    provider and credential are immutable — recreate the model to change them."""

    provider_model_id: str | None = None
    params: dict[str, Any] | None = None
    api_version: str | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    enabled: bool | None = None


@dataclass(frozen=True)
class ModelResponse:
    id: UUID
    team_id: UUID
    name: str
    provider: Provider
    credential_id: UUID
    type: ModelType
    provider_model_id: str
    params: dict[str, Any]
    api_version: str | None
    input_cost_per_token: float | None
    output_cost_per_token: float | None
    enabled: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, model: Model) -> ModelResponse:
        return cls(
            id=model.id,
            team_id=model.team_id,
            name=model.name,
            provider=model.provider,
            credential_id=model.credential_id,
            type=model.type,
            provider_model_id=model.provider_model_id,
            params=model.params,
            api_version=model.api_version,
            input_cost_per_token=model.input_cost_per_token,
            output_cost_per_token=model.output_cost_per_token,
            enabled=model.enabled,
            created_at=model.created_at,
        )
