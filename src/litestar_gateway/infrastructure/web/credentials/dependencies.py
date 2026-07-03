"""Dependency wiring for the credential service (needs DB session + keyring)."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.application.credential_service import CredentialService
from litestar_test.infrastructure.keyring import Keyring
from litestar_test.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)


def provide_credential_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
) -> CredentialService:
    return CredentialService(SQLAlchemyCredentialRepository(db_session, keyring))
