"""Resolve the acting Principal: a human JWT or a team service-principal key.

Used by the team-scoped management endpoints that accept both. The bearer
token's shape picks the path (JWTs are three dot-separated segments; API keys
never contain dots), so a wrong secret always fails with the error of its own
kind. Authorization (which team, which scope) happens in the services — this
dependency only authenticates.
"""

from __future__ import annotations

from litestar import Request
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.application.service_principal_service import ServicePrincipalService
from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import InvalidAPIKey
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_user


async def provide_principal(
    request: Request,
    keyring: NamedDependency[Keyring],
    user_service: NamedDependency[UserService],
    api_key_service: NamedDependency[APIKeyService],
    service_principal_service: NamedDependency[ServicePrincipalService],
) -> Principal:
    auth = request.headers.get("Authorization")
    if not auth:
        raise NotAuthorizedException("Missing bearer token")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise NotAuthorizedException("Invalid Authorization header")

    if token.count(".") == 2:  # a JWT — reuse the session path (all its checks)
        user = await provide_current_user(request, keyring, user_service)
        return Principal(user=user)

    try:
        key = await api_key_service.authenticate(token)
    except InvalidAPIKey as exc:
        raise NotAuthorizedException(str(exc)) from exc
    # Load the owning service principal (if any) — it is the acting identity for
    # authorization (must be enabled) and audit attribution.
    sp = None
    if key.service_principal_id is not None:
        sp = await service_principal_service.get(key.team_id, key.service_principal_id)
    return Principal(api_key=key, service_principal=sp)
