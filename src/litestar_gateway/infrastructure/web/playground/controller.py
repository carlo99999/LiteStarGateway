"""Playground: compare one prompt across several of a team's models.

Session-authenticated with a dedicated execution permission, so it runs
server-side without the browser handling an API key. Real provider calls are
budgeted, rate-limited, metered, traced, and audited — see PlaygroundService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from annotated_types import Ge, Le, MaxLen, MinLen
from litestar import Controller, Request, post
from litestar.di import NamedDependency, Provide
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.completion_service import CompletionService
from litestar_gateway.application.playground_service import (
    DEFAULT_MAX_MODELS,
    PlaygroundResult,
    PlaygroundService,
)
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.persistence.model_repository import SQLAlchemyModelRepository
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_user


def provide_playground_service(
    db_session: NamedDependency[AsyncSession],
    completion_service: NamedDependency[CompletionService],
    router_service: NamedDependency[RouterService],
) -> PlaygroundService:
    return PlaygroundService(
        models=SQLAlchemyModelRepository(db_session),
        completion_service=completion_service,
        routers=router_service,
    )


ModelAlias = Annotated[str, MinLen(1), MaxLen(200)]
ModelNames = Annotated[list[ModelAlias], MinLen(1), MaxLen(DEFAULT_MAX_MODELS)]
Messages = Annotated[list[dict[str, Any]], MinLen(1), MaxLen(100)]
MaxCompletionTokens = Annotated[int, Ge(1), Le(1_000_000)]


@dataclass(frozen=True)
class CompareRequest:
    team_id: UUID
    model_names: ModelNames
    messages: Messages
    max_completion_tokens: MaxCompletionTokens | None = None


@dataclass(frozen=True)
class PlaygroundResultResponse:
    model_name: str
    ok: bool
    content: str | None
    latency_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cost: float | None
    error: str | None
    chosen_model: str | None = None

    @classmethod
    def from_result(cls, r: PlaygroundResult) -> PlaygroundResultResponse:
        return cls(
            model_name=r.model_name,
            ok=r.ok,
            content=r.content,
            latency_ms=r.latency_ms,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            cost=r.cost,
            error=r.error,
            chosen_model=r.chosen_model,
        )


class PlaygroundController(Controller):
    path = "/playground"
    tags = ["playground"]
    dependencies = {
        "current_user": Provide(provide_current_user),
        "playground_service": Provide(provide_playground_service, sync_to_thread=False),
    }

    @post(
        "/compare",
        summary="Run one prompt against several of a team's models and compare",
        description=(
            "Sends the same messages to each model and returns the response, "
            "latency, token counts and estimated cost for each. Real provider "
            "calls consume the team's rate limit and budget and are recorded "
            "in usage and audit logs."
        ),
    )
    async def compare(
        self,
        request: Request,
        data: CompareRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        playground_service: NamedDependency[PlaygroundService],
        audit_log: NamedDependency[AuditLog],
    ) -> list[PlaygroundResultResponse]:
        await team_service.ensure_team_permission(
            current_user, data.team_id, Permission.PLAYGROUND_EXECUTE
        )
        results = await playground_service.compare(
            data.team_id, data.model_names, data.messages, data.max_completion_tokens
        )
        await record_audit(
            audit_log,
            request,
            current_user,
            "playground.compare",
            target_type="team",
            target_id=data.team_id,
            detail=(f"models={len(results)} succeeded={sum(result.ok for result in results)}"),
        )
        return [PlaygroundResultResponse.from_result(r) for r in results]
