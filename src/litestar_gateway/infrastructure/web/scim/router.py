"""Routers for the SCIM surface.

Two distinct auth realms, hence two routers: /scim/v2 authenticates the IdP via
a provisioning token, /scim-tokens authenticates a platform admin via JWT.
"""

from __future__ import annotations

from litestar.di import Provide
from litestar.router import Router

from litestar_gateway.domain.exceptions import DomainError
from litestar_gateway.infrastructure.web.rate_limit import build_scim_rate_limit
from litestar_gateway.infrastructure.web.scim.controller import (
    create_scim_user,
    delete_scim_user,
    get_scim_user,
    list_scim_users,
    patch_scim_user,
    replace_scim_user,
    scim_domain_exception_handler,
    service_provider_config,
)
from litestar_gateway.infrastructure.web.scim.dependencies import provide_scim_actor
from litestar_gateway.infrastructure.web.scim.tokens import (
    create_scim_token,
    list_scim_tokens,
    revoke_scim_token,
)


def create_scim_router() -> Router:
    """The IdP-facing SCIM 2.0 surface (provisioning-token auth)."""
    return Router(
        path="/scim/v2",
        route_handlers=[
            service_provider_config,
            create_scim_user,
            list_scim_users,
            get_scim_user,
            replace_scim_user,
            patch_scim_user,
            delete_scim_user,
        ],
        dependencies={"scim_actor": Provide(provide_scim_actor)},
        # SCIM clients expect RFC 7644 Error resources, not {"detail": ...}.
        exception_handlers={DomainError: scim_domain_exception_handler},
        # Per-IP guardrail: auth is a DB-backed token-hash lookup, so an
        # unauthenticated flood must be throttled before it reaches it (M49).
        middleware=[build_scim_rate_limit().middleware],
        tags=["scim"],
    )


def create_scim_tokens_router() -> Router:
    """Platform-admin management of the IdP's provisioning tokens (JWT auth)."""
    return Router(
        path="/",
        route_handlers=[create_scim_token, list_scim_tokens, revoke_scim_token],
        # Defense-in-depth: already JWT-gated, but the only admin surface that
        # otherwise had no limiter (M49).
        middleware=[build_scim_rate_limit().middleware],
        tags=["scim"],
    )
