"""SCIM 2.0 provisioning surface: /scim/v2 (IdP-facing) + /scim-tokens (admin)."""

from litestar_gateway.infrastructure.web.scim.router import (
    create_scim_router,
    create_scim_tokens_router,
)

__all__ = ["create_scim_router", "create_scim_tokens_router"]
