"""User-facing routes: public signup + admin-only invite creation."""

from __future__ import annotations

from litestar.router import Router

from litestar_gateway.infrastructure.web.users.invites import create_invite
from litestar_gateway.infrastructure.web.users.password_reset import (
    create_password_reset,
    reset_password,
)
from litestar_gateway.infrastructure.web.users.set_active import set_user_active
from litestar_gateway.infrastructure.web.users.set_admin import set_user_admin
from litestar_gateway.infrastructure.web.users.signup import signup
from litestar_gateway.infrastructure.web.users.unlock import unlock_user


def create_users_router() -> Router:
    # Admin-only handlers enforce the admin JWT via their own dependency; the
    # public ones (signup, reset-password) are rate-limited per IP.
    return Router(
        path="/",
        route_handlers=[
            signup,
            create_invite,
            create_password_reset,
            reset_password,
            set_user_active,
            set_user_admin,
            unlock_user,
        ],
        tags=["users"],
    )
