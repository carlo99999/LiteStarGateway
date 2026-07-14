"""Dependency wiring: build the UserService from a DB session."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.user_service import UserService
from litestar_gateway.infrastructure.persistence.invite_repository import (
    SQLAlchemyInviteRepository,
)
from litestar_gateway.infrastructure.persistence.membership_repository import (
    SQLAlchemyTeamMembershipRepository,
)
from litestar_gateway.infrastructure.persistence.password_reset_repository import (
    SQLAlchemyPasswordResetRepository,
)
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.team_repository import (
    SQLAlchemyTeamRepository,
)
from litestar_gateway.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def provide_user_service(db_session: NamedDependency[AsyncSession]) -> UserService:
    # Repositories and the unit-of-work share one request-scoped session, so
    # the service's single commit covers every staged write.
    return UserService(
        users=SQLAlchemyUserRepository(db_session),
        invites=SQLAlchemyInviteRepository(db_session),
        password_resets=SQLAlchemyPasswordResetRepository(db_session),
        transaction=db_session,
        teams=SQLAlchemyTeamRepository(db_session),
        api_keys=SQLAlchemyAPIKeyRepository(db_session),
        memberships=SQLAlchemyTeamMembershipRepository(db_session),
    )
