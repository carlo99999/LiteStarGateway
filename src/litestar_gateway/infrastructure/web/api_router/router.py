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
    embeddings,
    images,
    responses,
)
from litestar_test.infrastructure.web.api_router.wo_am_i import whoami
from litestar_test.infrastructure.web.auth import APIKeyAuthMiddleware
from litestar_test.infrastructure.web.rate_limit import build_inference_rate_limit

API_PREFIX = "/"


def create_api_router(config: SQLAlchemyAsyncConfig) -> Router:
    # Rate limit (per API key) runs before auth, so floods are throttled cheaply.
    return Router(
        path=API_PREFIX,
        route_handlers=[whoami, chat_completions, responses, embeddings, images],
        middleware=[
            build_inference_rate_limit().middleware,
            DefineMiddleware(APIKeyAuthMiddleware, config=config),
        ],
        tags=["api-endpoint"],
    )
