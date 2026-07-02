"""Team-scoped model deployments (team-admin or platform-admin).

A model binds a provider + a credential (of the same provider) + a type
(chat/image/embeddings) + the upstream model id, plus optional default params,
endpoint overrides, per-token costs and an enabled flag.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_test.application.model_service import ModelService
from litestar_test.application.team_service import TeamService
from litestar_test.domain.entities import User
from litestar_test.domain.pagination import resolve_page
from litestar_test.domain.ports import AuditLog
from litestar_test.infrastructure.web.audit.recorder import record_audit
from litestar_test.infrastructure.web.models.schemas import (
    CreateModelRequest,
    ModelResponse,
    UpdateModelRequest,
)
from litestar_test.infrastructure.web.session.dependencies import provide_current_user


class ModelController(Controller):
    path = "/teams"
    tags = ["models"]
    dependencies = {"current_user": Provide(provide_current_user)}

    @post(
        "/{team_id:uuid}/models",
        summary="Create a model",
        description=(
            "Create a model deployment for the team. `provider` must match the "
            "referenced credential's provider, otherwise the request is rejected. "
            "`type` is one of `chat`, `image`, `embeddings`."
        ),
    )
    async def create_model(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: CreateModelRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> ModelResponse:
        await team_service.ensure_can_manage_team(current_user, team_id)
        model = await model_service.create(
            team_id=team_id,
            name=data.name,
            provider=data.provider,
            credential_id=data.credential_id,
            model_type=data.type,
            provider_model_id=data.provider_model_id,
            params=data.params,
            api_version=data.api_version,
            input_cost_per_token=data.input_cost_per_token,
            output_cost_per_token=data.output_cost_per_token,
            enabled=data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            current_user,
            "model.create",
            target_type="model",
            target_id=model.id,
            detail=f"{data.name} ({data.provider}/{data.provider_model_id}) in team {team_id}",
        )
        return ModelResponse.from_entity(model)

    @get("/{team_id:uuid}/models", summary="List the team's models")
    async def list_models(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        model_service: NamedDependency[ModelService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[ModelResponse]:
        await team_service.ensure_can_manage_team(current_user, team_id)
        page_limit, page_offset = resolve_page(limit, offset)
        models = await model_service.list_for_team(team_id, limit=page_limit, offset=page_offset)
        return [ModelResponse.from_entity(m) for m in models]

    @patch(
        "/{team_id:uuid}/models/{model_id:uuid}",
        summary="Update a model",
        description="Update mutable fields. Omitted/null fields are left unchanged.",
    )
    async def update_model(
        self,
        request: Request,
        team_id: FromPath[UUID],
        model_id: FromPath[UUID],
        data: UpdateModelRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> ModelResponse:
        await team_service.ensure_can_manage_team(current_user, team_id)
        model = await model_service.update(
            team_id,
            model_id,
            provider_model_id=data.provider_model_id,
            params=data.params,
            api_version=data.api_version,
            input_cost_per_token=data.input_cost_per_token,
            output_cost_per_token=data.output_cost_per_token,
            enabled=data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            current_user,
            "model.update",
            target_type="model",
            target_id=model_id,
            detail=f"team {team_id}",
        )
        return ModelResponse.from_entity(model)

    @delete("/{team_id:uuid}/models/{model_id:uuid}", summary="Delete a model")
    async def delete_model(
        self,
        request: Request,
        team_id: FromPath[UUID],
        model_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await team_service.ensure_can_manage_team(current_user, team_id)
        await model_service.delete(team_id, model_id)
        await record_audit(
            audit_log,
            request,
            current_user,
            "model.delete",
            target_type="model",
            target_id=model_id,
            detail=f"team {team_id}",
        )
