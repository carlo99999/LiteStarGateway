"""DTOs for model deployments (team-owned or global) and extension grants."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from litestar_gateway.application.model_service import CallableModel
from litestar_gateway.domain.entities import Model, ModelGrant, ModelType, Provider


@dataclass(frozen=True)
class CreateModelRequest:
    name: str
    provider: Provider
    credential_id: UUID  # must reference a credential of the same provider
    type: ModelType
    provider_model_id: str  # upstream model name, e.g. "gpt-4o"
    params: dict[str, Any] = field(default_factory=dict)  # client-overridable defaults
    params_enforced: dict[str, Any] = field(default_factory=dict)  # admin policy, not overridable
    max_output_tokens: int | None = None  # per-model output-token ceiling (min clamp)
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
    params_enforced: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    api_version: str | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    enabled: bool | None = None


@dataclass(frozen=True)
class ModelResponse:
    id: UUID
    team_id: UUID | None  # None ⇒ a global (platform) model
    name: str
    provider: Provider
    credential_id: UUID
    type: ModelType
    provider_model_id: str
    params: dict[str, Any]
    params_enforced: dict[str, Any]
    max_output_tokens: int | None
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
            params_enforced=model.params_enforced,
            max_output_tokens=model.max_output_tokens,
            api_version=model.api_version,
            input_cost_per_token=model.input_cost_per_token,
            output_cost_per_token=model.output_cost_per_token,
            enabled=model.enabled,
            created_at=model.created_at,
        )


@dataclass(frozen=True)
class ExtendModelRequest:
    """Extend a (team-owned) model to the given teams."""

    team_ids: list[UUID]


@dataclass(frozen=True)
class GrantResponse:
    """A model extended to a team, under `alias`."""

    id: UUID
    model_id: UUID
    team_id: UUID
    alias: str
    created_at: datetime

    @classmethod
    def from_entity(cls, grant: ModelGrant) -> GrantResponse:
        return cls(
            id=grant.id,
            model_id=grant.model_id,
            team_id=grant.team_id,
            alias=grant.alias,
            created_at=grant.created_at,
        )


@dataclass(frozen=True)
class CallableModelResponse:
    """A model a team can call, with its effective alias and provenance."""

    alias: str
    origin: str  # own | extended | global
    source_team_id: UUID | None
    model: ModelResponse

    @classmethod
    def from_entity(cls, callable_model: CallableModel) -> CallableModelResponse:
        return cls(
            alias=callable_model.alias,
            origin=callable_model.origin,
            source_team_id=callable_model.source_team_id,
            model=ModelResponse.from_entity(callable_model.model),
        )
