"""Dependency wiring for the credential service (needs DB session + cipher)."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.application.credential_service import CredentialService
from litestar_test.infrastructure.crypto import CredentialCipher
from litestar_test.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)


def provide_credential_service(
    db_session: NamedDependency[AsyncSession],
    credential_cipher: NamedDependency[CredentialCipher],
) -> CredentialService:
    return CredentialService(SQLAlchemyCredentialRepository(db_session, credential_cipher))
