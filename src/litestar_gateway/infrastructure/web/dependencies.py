"""Dependency wiring: build the application service from a DB session."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.service_principal_repository import (
    SQLAlchemyServicePrincipalRepository,
)


def provide_api_key_service(
    db_session: NamedDependency[AsyncSession],
) -> APIKeyService:
    return APIKeyService(
        SQLAlchemyAPIKeyRepository(db_session),
        SQLAlchemyServicePrincipalRepository(db_session),
    )
