"""Admin web adapter for smart routers (virtual models)."""

from litestar_gateway.infrastructure.web.routing.controller import (
    RouterController,
    platform_routing_savings,
)
from litestar_gateway.infrastructure.web.routing.platform_controller import (
    PlatformRouterController,
)

__all__ = ["PlatformRouterController", "RouterController", "platform_routing_savings"]
