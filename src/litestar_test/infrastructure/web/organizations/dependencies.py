"""Dependency wiring for organization and team services."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.application.organization_service import OrganizationService
from litestar_test.application.team_service import TeamService
from litestar_test.infrastructure.persistence.membership_repository import (
    SQLAlchemyTeamMembershipRepository,
)
from litestar_test.infrastructure.persistence.organization_repository import (
    SQLAlchemyOrganizationRepository,
)
from litestar_test.infrastructure.persistence.team_repository import (
    SQLAlchemyTeamRepository,
)
from litestar_test.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def provide_organization_service(
    db_session: NamedDependency[AsyncSession],
) -> OrganizationService:
    return OrganizationService(SQLAlchemyOrganizationRepository(db_session))


def provide_team_service(db_session: NamedDependency[AsyncSession]) -> TeamService:
    return TeamService(
        organizations=SQLAlchemyOrganizationRepository(db_session),
        teams=SQLAlchemyTeamRepository(db_session),
        memberships=SQLAlchemyTeamMembershipRepository(db_session),
        users=SQLAlchemyUserRepository(db_session),
    )
