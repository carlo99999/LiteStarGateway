"""Dependency wiring: build the application service from a DB session."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.infrastructure.persistence.api_key_budget_repository import (
    SQLAlchemyApiKeyBudgetRepository,
)
from litestar_gateway.infrastructure.persistence.audit_repository import SQLAlchemyAuditLog
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.service_principal_repository import (
    SQLAlchemyServicePrincipalRepository,
)
from litestar_gateway.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def provide_api_key_service(
    db_session: NamedDependency[AsyncSession],
) -> APIKeyService:
    return APIKeyService(
        SQLAlchemyAPIKeyRepository(db_session),
        transaction=db_session,
        users=SQLAlchemyUserRepository(db_session),
        service_principals=SQLAlchemyServicePrincipalRepository(db_session),
        audit_log=SQLAlchemyAuditLog(db_session),
        # Rotation carries the key's spend cap onto its replacement.
        api_key_budgets=SQLAlchemyApiKeyBudgetRepository(db_session),
    )
