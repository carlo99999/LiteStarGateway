"""SQLAlchemy adapter for credentials — encrypts values at rest via the keyring.

Values are encrypted with the active credential data key (envelope encryption);
each row records the `key_id` that encrypted it, so rotation can re-encrypt while
old rows stay readable.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Credential
from litestar_gateway.domain.exceptions import (
    CredentialMisconfigured,
    CredentialNameExists,
    SaltKeyMissing,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.orm import CredentialModel


class SQLAlchemyCredentialRepository:
    def __init__(self, session: AsyncSession, keyring: Keyring | None = None) -> None:
        # `keyring` is only needed to encrypt/decrypt values. Metadata reads
        # (get/get_by_name/list) work without it — useful for provider validation.
        self._session = session
        self._keyring = keyring

    def _require_keyring(self) -> Keyring:
        if self._keyring is None:
            raise SaltKeyMissing("SALT_KEY is not configured")
        return self._keyring

    async def add(self, credential: Credential, values: dict[str, str]) -> Credential:
        key_id, cipher = await self._require_keyring().active_credential_cipher()
        model = CredentialModel(
            id=credential.id,
            name=credential.name,
            provider=credential.provider.value,
            encrypted_values=cipher.encrypt(values),
            key_id=key_id,
        )
        self._session.add(model)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            # Loser of a concurrent create with the same name: the service's
            # pre-check passed for both, the unique constraint catches the race.
            await self._session.rollback()
            raise CredentialNameExists(credential.name) from exc
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

    async def list(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Credential]:
        models = await self._session.scalars(
            select(CredentialModel).order_by(CredentialModel.created_at).limit(limit).offset(offset)
        )
        return [m.to_entity() for m in models]

    async def get_values(self, credential_id: UUID) -> dict[str, str] | None:
        model = await self._session.get(CredentialModel, credential_id)
        if model is None:
            return None
        cipher = await self._require_keyring().credential_cipher_for(model.key_id)
        if cipher is None:  # pragma: no cover - a missing key row is not expected
            raise CredentialMisconfigured("encryption key for credential is missing")
        return cipher.decrypt(model.encrypted_values)

    async def reencrypt_all(self) -> None:
        """Re-encrypt every credential with the active data key (rotation)."""
        keyring = self._require_keyring()
        new_key_id, new_cipher = await keyring.active_credential_cipher()
        models = list(await self._session.scalars(select(CredentialModel)))
        for model in models:
            if model.key_id == new_key_id:
                continue
            old = await keyring.credential_cipher_for(model.key_id)
            if old is None:  # pragma: no cover
                continue
            model.encrypted_values = new_cipher.encrypt(old.decrypt(model.encrypted_values))
            model.key_id = new_key_id
        await self._session.commit()

    async def remove(self, credential_id: UUID) -> None:
        await self._session.execute(
            delete(CredentialModel).where(CredentialModel.id == credential_id)
        )
        await self._session.commit()
