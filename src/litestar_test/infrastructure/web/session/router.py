"""Session routes: public login + JWT-protected /me."""

from __future__ import annotations

from litestar.router import Router

from litestar_test.infrastructure.web.session.login import login
from litestar_test.infrastructure.web.session.me import me


def create_session_router() -> Router:
    return Router(path="/", route_handlers=[login, me], tags=["session"])
