"""Dependency wiring for the credential service (needs DB session + keyring)."""

from __future__ import annotations

from collections.abc import Callable

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.credential_service import CredentialService
from litestar_gateway.config import Settings
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)


def build_credential_service_provider(
    settings: Settings,
) -> Callable[[AsyncSession, Keyring], CredentialService]:
    """The service is per-request (it holds a session), but the egress allowlist
    is static config — parsed once at app build time rather than on every
    credential write."""
    allowlist = settings.egress_allowlist()

    def provide_credential_service(
        db_session: NamedDependency[AsyncSession],
        keyring: NamedDependency[Keyring],
    ) -> CredentialService:
        return CredentialService(
            SQLAlchemyCredentialRepository(db_session, keyring),
            SQLAlchemyModelRepository(db_session),
            egress_allowlist=allowlist,
        )

    return provide_credential_service
