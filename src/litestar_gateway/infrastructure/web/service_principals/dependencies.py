"""Dependency wiring for service principals."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.application.service_principal_service import ServicePrincipalService
from litestar_gateway.infrastructure.persistence.repository import SQLAlchemyAPIKeyRepository
from litestar_gateway.infrastructure.persistence.service_principal_repository import (
    SQLAlchemyServicePrincipalRepository,
)


def provide_service_principal_service(
    db_session: NamedDependency[AsyncSession],
) -> ServicePrincipalService:
    return ServicePrincipalService(
        SQLAlchemyServicePrincipalRepository(db_session),
        APIKeyService(SQLAlchemyAPIKeyRepository(db_session)),
    )
