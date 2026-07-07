"""Dependency wiring: build the RouterService from a DB session."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.routing.service import RouterService
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
