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

from litestar_gateway.application.service import APIKeyService
from litestar_gateway.domain.exceptions import InvalidAPIKey
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.service_principal_repository import (
    SQLAlchemyServicePrincipalRepository,
)


def _extract_key(connection: ASGIConnection) -> str | None:
    if auth := connection.headers.get("Authorization"):
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token
    # Native provider SDKs authenticate with their own header: the `anthropic`
    # SDK's `api_key=...` sends `x-api-key`. Accept it as the same gateway key so
    # the native `/v1/messages` endpoint works with the stock client, without a
    # second auth realm.
    if x_api_key := connection.headers.get("x-api-key"):
        return x_api_key
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
            service = APIKeyService(
                SQLAlchemyAPIKeyRepository(session),
                SQLAlchemyServicePrincipalRepository(session),
            )
            try:
                key = await service.authenticate(plaintext)
            except InvalidAPIKey as exc:
                raise NotAuthorizedException(str(exc)) from exc
            # A management-only key is a service principal for the admin
            # surface — it must not spend on the inference endpoints.
            if not key.scope.allows_inference:
                raise NotAuthorizedException("API key lacks inference scope")
            return AuthenticationResult(user=str(key.team_id), auth=key)
