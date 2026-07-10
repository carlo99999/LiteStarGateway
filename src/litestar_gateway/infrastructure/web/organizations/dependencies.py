"""Dependency wiring for organization and team services."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.organization_service import OrganizationService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.infrastructure.persistence.membership_repository import (
    SQLAlchemyTeamMembershipRepository,
)
from litestar_gateway.infrastructure.persistence.organization_repository import (
    SQLAlchemyOrganizationRepository,
)
from litestar_gateway.infrastructure.persistence.team_repository import (
    SQLAlchemyTeamRepository,
)
from litestar_gateway.infrastructure.persistence.usage_repository import (
    SQLAlchemyUsageRepository,
)
from litestar_gateway.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def provide_organization_service(
    db_session: NamedDependency[AsyncSession],
) -> OrganizationService:
    return OrganizationService(
        SQLAlchemyOrganizationRepository(db_session),
        SQLAlchemyTeamRepository(db_session),
        SQLAlchemyUsageRepository(db_session),
    )


def provide_team_service(db_session: NamedDependency[AsyncSession]) -> TeamService:
    # The repositories and the unit-of-work share one request-scoped session, so
    # the service's single commit covers every staged write.
    return TeamService(
        organizations=SQLAlchemyOrganizationRepository(db_session),
        teams=SQLAlchemyTeamRepository(db_session),
        memberships=SQLAlchemyTeamMembershipRepository(db_session),
        users=SQLAlchemyUserRepository(db_session),
        transaction=db_session,
    )
