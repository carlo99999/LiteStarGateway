"""Dependency wiring for the SCIM surface: service + provisioning-token auth."""

from __future__ import annotations

from litestar import Request
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.scim_service import ScimService
from litestar_gateway.domain.entities import ScimToken
from litestar_gateway.domain.exceptions import InvalidScimToken
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.scim_token_repository import (
    SQLAlchemyScimTokenRepository,
)
from litestar_gateway.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def provide_scim_service(db_session: NamedDependency[AsyncSession]) -> ScimService:
    return ScimService(
        users=SQLAlchemyUserRepository(db_session),
        tokens=SQLAlchemyScimTokenRepository(db_session),
        transaction=db_session,
        api_keys=SQLAlchemyAPIKeyRepository(db_session),
    )


async def provide_scim_actor(
    request: Request, scim_service: NamedDependency[ScimService]
) -> ScimToken:
    """Authenticate the IdP via `Authorization: Bearer <scim token>`.

    Only admin-minted provisioning tokens pass — login JWTs and `lsk_` API keys
    hash to nothing in the scim_token table and are rejected."""
    auth = request.headers.get("Authorization")
    if not auth:
        raise NotAuthorizedException("Missing SCIM bearer token")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise NotAuthorizedException("Invalid Authorization header")
    try:
        return await scim_service.authenticate(token)
    except InvalidScimToken as exc:
        raise NotAuthorizedException(str(exc)) from exc
