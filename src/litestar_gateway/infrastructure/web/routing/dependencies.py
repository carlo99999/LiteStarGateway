"""Dependency wiring: RouterService + the shadow-mode own-session factories."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.ports import (
    CallableModelResolver,
    CredentialRepository,
    ModelRepository,
    RoutingDecisionLog,
    RoutingDecisionLogFactory,
    RoutingRepositoryFactory,
)
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.callable_alias_repository import (
    SQLAlchemyCallableAliasRepository,
)
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


def provide_router_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
    callable_resolver: NamedDependency[CallableAliasResolver],
) -> RouterService:
    routers = SQLAlchemyRouterRepository(db_session, keyring)
    models = SQLAlchemyModelRepository(db_session)
    return RouterService(
        routers=routers,
        models=models,
        decisions=SQLAlchemyRoutingDecisionLog(db_session),
        callable_resolver=callable_resolver,
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
    async def open_repos() -> AsyncIterator[
        tuple[ModelRepository, CredentialRepository, CallableModelResolver]
    ]:
        async with session_maker() as session:
            models = SQLAlchemyModelRepository(session)
            keyring = keyring_factory(session)
            yield (
                models,
                SQLAlchemyCredentialRepository(session, keyring),
                CallableAliasResolver(
                    SQLAlchemyCallableAliasRepository(session),
                    models,
                    SQLAlchemyRouterRepository(session, keyring),
                ),
            )

    return open_repos
