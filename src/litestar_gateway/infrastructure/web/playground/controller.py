"""Playground: compare one prompt across several of a team's models.

Session-authenticated (a team member with model-read access), so it runs
server-side without the browser handling an API key. Real provider calls, not
metered — see PlaygroundService.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from litestar import Controller, post
from litestar.di import NamedDependency, Provide
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.playground_service import PlaygroundResult, PlaygroundService
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.ports import LLMGateway
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import SQLAlchemyModelRepository
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_user


def provide_playground_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
    llm_gateway: NamedDependency[LLMGateway],
    router_service: NamedDependency[RouterService],
) -> PlaygroundService:
    return PlaygroundService(
        models=SQLAlchemyModelRepository(db_session),
        credentials=SQLAlchemyCredentialRepository(db_session, keyring),
        gateway=llm_gateway,
        routers=router_service,
    )


@dataclass(frozen=True)
class CompareRequest:
    team_id: UUID
    model_names: list[str]
    messages: list[dict[str, Any]]
    max_completion_tokens: int | None = None


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
            "Sends the same messages to each model concurrently and returns the "
            "response, latency, token counts and estimated cost for each. Real "
            "provider calls, but not metered/billed."
        ),
    )
    async def compare(
        self,
        data: CompareRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        playground_service: NamedDependency[PlaygroundService],
    ) -> list[PlaygroundResultResponse]:
        await team_service.ensure_team_permission(
            current_user, data.team_id, Permission.MODELS_READ
        )
        results = await playground_service.compare(
            data.team_id, data.model_names, data.messages, data.max_completion_tokens
        )
        return [PlaygroundResultResponse.from_result(r) for r in results]
