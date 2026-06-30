"""User-facing routes: public signup + admin-only invite creation."""

from __future__ import annotations

from litestar.router import Router

from litestar_test.infrastructure.web.users.invites import create_invite
from litestar_test.infrastructure.web.users.signup import signup


def create_users_router() -> Router:
    # Invite creation requires an admin JWT (enforced by its own dependency).
    return Router(path="/", route_handlers=[signup, create_invite], tags=["users"])
