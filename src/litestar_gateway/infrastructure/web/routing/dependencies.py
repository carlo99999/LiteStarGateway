"""Dependency wiring: RouterService + the shadow-mode decision-log factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.domain.ports import RoutingDecisionLog, RoutingDecisionLogFactory
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
