"""Dependency wiring: RouterService + the shadow-mode own-session factories."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.ports import (
    CredentialRepository,
    ModelRepository,
    RoutingDecisionLog,
    RoutingDecisionLogFactory,
    RoutingRepositoryFactory,
)
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)
from litestar_gateway.infrastructure.persistence.router_repository import (
    SQLAlchemyRouterRepository,
    SQLAlchemyRoutingDecisionLog,
)


def provide_router_service(db_session: NamedDependency[AsyncSession]) -> RouterService:
    return RouterService(
        routers=SQLAlchemyRouterRepository(db_session),
        models=SQLAlchemyModelRepository(db_session),
        decisions=SQLAlchemyRoutingDecisionLog(db_session),
    )


def make_shadow_log_factory(session_maker: Any) -> RoutingDecisionLogFactory:
    """A decision log with its own session/unit of work: shadow tasks outlive
    the request, whose scoped session is closed by the time they persist."""

    @asynccontextmanager
    async def open_log() -> AsyncIterator[RoutingDecisionLog]:
        async with session_maker() as session:
            yield SQLAlchemyRoutingDecisionLog(session)

    return open_log


def make_shadow_repos_factory(
    session_maker: Any, keyring_factory: Callable[[AsyncSession], Keyring]
) -> RoutingRepositoryFactory:
    """Model/credential repositories with their own session/unit of work: the
    shadow strategy's DB lookups (judge/embeddings) race the request coroutine,
    which is still issuing statements on the request-scoped session."""

    @asynccontextmanager
    async def open_repos() -> AsyncIterator[tuple[ModelRepository, CredentialRepository]]:
        async with session_maker() as session:
            yield (
                SQLAlchemyModelRepository(session),
                SQLAlchemyCredentialRepository(session, keyring_factory(session)),
            )

    return open_repos
