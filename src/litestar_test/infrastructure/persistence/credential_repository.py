"""SQLAlchemy adapter for credentials — encrypts values at rest via the cipher."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_test.domain.entities import Credential
from litestar_test.domain.exceptions import SaltKeyMissing
from litestar_test.infrastructure.crypto import CredentialCipher
from litestar_test.infrastructure.persistence.orm import CredentialModel


class SQLAlchemyCredentialRepository:
    def __init__(self, session: AsyncSession, cipher: CredentialCipher | None = None) -> None:
        # `cipher` is only needed to encrypt/decrypt values. Metadata reads
        # (get/get_by_name/list) work without it — useful for provider validation.
        self._session = session
        self._cipher = cipher

    def _require_cipher(self) -> CredentialCipher:
        if self._cipher is None:
            raise SaltKeyMissing("SALT_KEY is not configured")
        return self._cipher

    async def add(self, credential: Credential, values: dict[str, str]) -> Credential:
        model = CredentialModel(
            id=credential.id,
            name=credential.name,
            provider=credential.provider.value,
            encrypted_values=self._require_cipher().encrypt(values),
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, credential_id: UUID) -> Credential | None:
        model = await self._session.get(CredentialModel, credential_id)
        return model.to_entity() if model else None

    async def get_by_name(self, name: str) -> Credential | None:
        model = await self._session.scalar(
            select(CredentialModel).where(CredentialModel.name == name)
        )
        return model.to_entity() if model else None

    async def list(self) -> list[Credential]:
        models = await self._session.scalars(
            select(CredentialModel).order_by(CredentialModel.created_at)
        )
        return [m.to_entity() for m in models]

    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        model = await self._session.get(CredentialModel, credential_id)
        return self._require_cipher().decrypt(model.encrypted_values) if model else None

    async def remove(self, credential_id: UUID) -> None:
        await self._session.execute(
            delete(CredentialModel).where(CredentialModel.id == credential_id)
        )
        await self._session.commit()
