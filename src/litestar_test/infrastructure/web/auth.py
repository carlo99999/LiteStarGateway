"""Authentication adapter: validates an API key per request.

The middleware runs outside Litestar's DI scope, so it builds its own session
from the running app's session maker. It reads the session-maker key from the
*same* config instance used to build the app — otherwise AA's per-config key
suffixing (e.g. `session_maker_class_1`) would not match.
"""

from __future__ import annotations

from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig
from litestar.connection import ASGIConnection
from litestar.exceptions import NotAuthorizedException
from litestar.middleware import AbstractAuthenticationMiddleware, AuthenticationResult
from litestar.types import ASGIApp

from litestar_test.application.service import APIKeyService
from litestar_test.domain.exceptions import InvalidAPIKey
from litestar_test.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)


def _extract_key(connection: ASGIConnection) -> str | None:
    if auth := connection.headers.get("Authorization"):
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    return None


class APIKeyAuthMiddleware(AbstractAuthenticationMiddleware):
    def __init__(self, app: ASGIApp, config: SQLAlchemyAsyncConfig, **kwargs: object) -> None:
        super().__init__(app, **kwargs)  # type: ignore[arg-type]
        self._config = config

    async def authenticate_request(self, connection: ASGIConnection) -> AuthenticationResult:
        plaintext = _extract_key(connection)
        if not plaintext:
            raise NotAuthorizedException("Missing API key")

        session_maker = connection.app.state[self._config.session_maker_app_state_key]
        async with session_maker() as session:
            service = APIKeyService(SQLAlchemyAPIKeyRepository(session))
            try:
                key = await service.authenticate(plaintext)
            except InvalidAPIKey as exc:
                raise NotAuthorizedException(str(exc)) from exc
            return AuthenticationResult(user=str(key.team_id), auth=key)
