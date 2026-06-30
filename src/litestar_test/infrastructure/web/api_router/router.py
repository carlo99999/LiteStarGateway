"""The protected "api-endpoint" group.

Every handler registered on this router requires a valid API key, because the
auth middleware is attached to the router itself — not to the whole app. Public
routes (health) and admin routes (key management) live outside it and stay open.

To add a protected endpoint, register it here.
"""

from __future__ import annotations

from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig
from litestar.middleware.base import DefineMiddleware
from litestar.router import Router

from litestar_test.infrastructure.web.api_router.completions import (
    chat_completions,
    responses,
)
from litestar_test.infrastructure.web.api_router.wo_am_i import whoami
from litestar_test.infrastructure.web.auth import APIKeyAuthMiddleware

API_PREFIX = "/"


def create_api_router(config: SQLAlchemyAsyncConfig) -> Router:
    return Router(
        path=API_PREFIX,
        route_handlers=[whoami, chat_completions, responses],
        middleware=[DefineMiddleware(APIKeyAuthMiddleware, config=config)],
        tags=["api-endpoint"],
    )
