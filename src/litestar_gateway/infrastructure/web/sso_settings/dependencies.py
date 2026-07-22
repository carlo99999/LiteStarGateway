"""Dependency wiring for the SSO settings service (needs DB session + keyring)."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.sso_settings_service import SsoSettingsService
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.sso_settings_repository import (
    SQLAlchemySsoSettingsRepository,
)


def provide_sso_settings_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
) -> SsoSettingsService:
    return SsoSettingsService(SQLAlchemySsoSettingsRepository(db_session, keyring))
