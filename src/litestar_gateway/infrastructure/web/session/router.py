"""Session routes: public login + JWT-protected /me."""

from __future__ import annotations

from litestar.router import Router

from litestar_gateway.infrastructure.web.session.browser import (
    browser_login,
    browser_logout,
    get_browser_session,
)
from litestar_gateway.infrastructure.web.session.login import login
from litestar_gateway.infrastructure.web.session.logout import logout
from litestar_gateway.infrastructure.web.session.me import me, my_teams


def create_session_router() -> Router:
    return Router(
        path="/",
        route_handlers=[
            login,
            logout,
            me,
            my_teams,
            browser_login,
            browser_logout,
            get_browser_session,
        ],
        tags=["session"],
    )
