"""Startup bootstrap: ensure an admin user exists.

Runs as an `on_startup` hook (after Advanced Alchemy has created the tables).
If the users table is empty and no MASTER_KEY is set, `ensure_admin` raises and
the app fails to start — by design.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from litestar import Litestar

from litestar_test.application.user_service import UserService
from litestar_test.config import Settings
from litestar_test.infrastructure.persistence.database import Database
from litestar_test.infrastructure.persistence.invite_repository import (
    SQLAlchemyInviteRepository,
)
from litestar_test.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def make_bootstrap_admin(
    database: Database, settings: Settings
) -> Callable[[Litestar], Coroutine[Any, Any, None]]:
    async def bootstrap_admin(app: Litestar) -> None:
        session_maker = app.state[database.config.session_maker_app_state_key]
        async with session_maker() as session:
            service = UserService(
                users=SQLAlchemyUserRepository(session),
                invites=SQLAlchemyInviteRepository(session),
            )
            await service.ensure_admin(settings.admin_email, settings.master_key)

    return bootstrap_admin
