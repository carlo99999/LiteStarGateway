"""Platform-admin model management: global models + extension grants.

Global models (`team_id is None`) are callable by every team, present and
future. "Extending" a team-owned model shares the *same* model with other teams
(a grant, not a copy) — the source stays the single source of truth for costs
and config. All routes here require a platform admin.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, delete, get, patch, post
from litestar.di import NamedDependency, Provide
from litestar.exceptions import ClientException
from litestar.params import FromPath, FromQuery

from litestar_gateway.application.model_service import ModelService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.models.schemas import (
    CreateModelRequest,
    ExtendModelRequest,
    GrantResponse,
    ModelResponse,
    UpdateModelRequest,
)
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin


class PlatformModelController(Controller):
    path = "/platform/models"
    tags = ["models"]
    dependencies = {"current_admin": Provide(provide_current_admin)}

    @post("", summary="Create a global (platform) model callable by every team")
    async def create_global(
        self,
        request: Request,
        data: CreateModelRequest,
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> ModelResponse:
        model = await model_service.create(
            team_id=None,
            name=data.name,
            provider=data.provider,
            credential_id=data.credential_id,
            model_type=data.type,
            provider_model_id=data.provider_model_id,
            params=data.params,
            params_enforced=data.params_enforced,
            max_output_tokens=data.max_output_tokens,
            api_version=data.api_version,
            input_cost_per_token=data.input_cost_per_token,
            output_cost_per_token=data.output_cost_per_token,
            enabled=data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "model.create_global",
            target_type="model",
            target_id=model.id,
            detail=f"{data.name} ({data.provider}/{data.provider_model_id})",
        )
        return ModelResponse.from_entity(model)

    @get("", summary="List global (platform) models")
    async def list_global(
        self,
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[ModelResponse]:
        page_limit, page_offset = resolve_page(limit, offset)
        models = await model_service.list_global(limit=page_limit, offset=page_offset)
        return [ModelResponse.from_entity(m) for m in models]

    @patch("/{model_id:uuid}", summary="Update a global model")
    async def update_global(
        self,
        request: Request,
        model_id: FromPath[UUID],
        data: UpdateModelRequest,
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> ModelResponse:
        model = await model_service.update(
            None,
            model_id,
            provider_model_id=data.provider_model_id,
            params=data.params,
            params_enforced=data.params_enforced,
            max_output_tokens=data.max_output_tokens,
            api_version=data.api_version,
            input_cost_per_token=data.input_cost_per_token,
            output_cost_per_token=data.output_cost_per_token,
            enabled=data.enabled,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "model.update_global",
            target_type="model",
            target_id=model_id,
            detail="global",
        )
        return ModelResponse.from_entity(model)

    @delete("/{model_id:uuid}", summary="Delete a global model")
    async def delete_global(
        self,
        request: Request,
        model_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await model_service.delete(None, model_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "model.delete_global",
            target_type="model",
            target_id=model_id,
            detail="global",
        )

    @post("/{model_id:uuid}/make-global", summary="Promote a team model to global")
    async def make_global(
        self,
        request: Request,
        model_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> ModelResponse:
        model = await model_service.make_global(model_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "model.make_global",
            target_type="model",
            target_id=model_id,
            detail=model.name,
        )
        return ModelResponse.from_entity(model)

    @post("/{model_id:uuid}/extend", summary="Extend a team model to other teams")
    async def extend(
        self,
        request: Request,
        model_id: FromPath[UUID],
        data: ExtendModelRequest,
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> list[GrantResponse]:
        model = await model_service.get_any(model_id)
        if model.team_id is None:
            # A global model is already callable everywhere; nothing to extend.
            raise ClientException("A global model is already available to every team")
        source_team = await team_service.get_team(current_admin, model.team_id)
        grants = await model_service.extend(model_id, source_team.name, data.team_ids)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "model.extend",
            target_type="model",
            target_id=model_id,
            detail=f"{model.name} → {len(grants)} team(s)",
        )
        return [GrantResponse.from_entity(g) for g in grants]

    @get("/{model_id:uuid}/grants", summary="Teams a model is extended to")
    async def list_grants(
        self,
        model_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
    ) -> list[GrantResponse]:
        grants = await model_service.list_grants(model_id)
        return [GrantResponse.from_entity(g) for g in grants]

    @delete("/grants/{grant_id:uuid}", summary="Un-extend (revoke a grant)")
    async def unextend(
        self,
        request: Request,
        grant_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        model_service: NamedDependency[ModelService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await model_service.unextend(grant_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "model.unextend",
            target_type="model_grant",
            target_id=grant_id,
            detail="revoked",
        )
