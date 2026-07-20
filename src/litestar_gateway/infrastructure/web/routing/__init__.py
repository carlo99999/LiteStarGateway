"""Admin web adapter for smart routers (virtual models)."""

from litestar_gateway.infrastructure.web.routing.controller import (
    RouterController,
    platform_routing_savings,
)

__all__ = ["RouterController", "platform_routing_savings"]
