"""The protected "api-endpoint" group.

Every handler registered on this router requires a valid API key, because the
auth middleware is attached to the router itself — not to the whole app. Public
routes (health) and admin routes (key management) live outside it and stay open.

To add a protected endpoint, register it here.
"""

from __future__ import annotations

from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig
from litestar.exceptions import HTTPException
from litestar.middleware.base import DefineMiddleware
from litestar.router import Router

from litestar_gateway.domain.exceptions import DomainError
from litestar_gateway.infrastructure.web.api_router.completions import (
    chat_completions,
    embeddings,
    images,
    responses,
)
from litestar_gateway.infrastructure.web.api_router.models_list import list_models
from litestar_gateway.infrastructure.web.api_router.wo_am_i import whoami
from litestar_gateway.infrastructure.web.auth import APIKeyAuthMiddleware
from litestar_gateway.infrastructure.web.exception_handlers import (
    openai_error_handler,
    openai_http_exception_handler,
)
from litestar_gateway.infrastructure.web.native.controller import native_messages
from litestar_gateway.infrastructure.web.native.gemini import generate_content
from litestar_gateway.infrastructure.web.rate_limit import build_inference_rate_limit

API_PREFIX = "/"


def create_api_router(config: SQLAlchemyAsyncConfig) -> Router:
    # Rate limit (per API key) runs before auth, so floods are throttled cheaply.
    return Router(
        path=API_PREFIX,
        route_handlers=[
            whoami,
            list_models,
            chat_completions,
            responses,
            embeddings,
            images,
            native_messages,
            generate_content,
        ],
        middleware=[
            build_inference_rate_limit().middleware,
            DefineMiddleware(APIKeyAuthMiddleware, config=config),
        ],
        # Router-level handlers override the app-level {"detail": ...} shape for
        # these routes only, so /v1/* emits the OpenAI error envelope while the
        # management API keeps its {"detail": ...} shape. DomainError covers the
        # route handlers; HTTPException covers the pre-handler middleware (auth
        # 401, rate-limit 429, body-size 413) that raise Litestar's own errors.
        exception_handlers={
            DomainError: openai_error_handler,
            HTTPException: openai_http_exception_handler,
        },
        tags=["api-endpoint"],
    )
