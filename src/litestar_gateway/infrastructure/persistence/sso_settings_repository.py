"""SQLAlchemy adapter for the SSO settings singleton — encrypts the client
secret at rest via the keyring, same envelope scheme as provider credentials.

At most one row ever exists; enforced here (get-the-only-row / upsert), not
by a DB-level singleton constraint (see the migration's comment).
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import SsoSettings, TeamGrant, team_mapping_to_json
from litestar_gateway.domain.exceptions import CredentialMisconfigured, SaltKeyMissing
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.orm import SsoSettingsModel


class SQLAlchemySsoSettingsRepository:
    def __init__(self, session: AsyncSession, keyring: Keyring | None = None) -> None:
        # `keyring` is only needed to encrypt/decrypt the client secret. `get()`
        # (metadata only) works without it.
        self._session = session
        self._keyring = keyring

    def _require_keyring(self) -> Keyring:
        if self._keyring is None:
            raise SaltKeyMissing("SALT_KEY is not configured")
        return self._keyring

    async def _get_model(self) -> SsoSettingsModel | None:
        return await self._session.scalar(select(SsoSettingsModel).limit(1))

    async def get(self) -> SsoSettings | None:
        model = await self._get_model()
        return model.to_entity() if model else None

    async def get_client_secret(self) -> str | None:
        model = await self._get_model()
        if model is None or model.encrypted_client_secret is None or model.key_id is None:
            return None
        cipher = await self._require_keyring().credential_cipher_for(model.key_id)
        if cipher is None:  # pragma: no cover - a missing key row is not expected
            raise CredentialMisconfigured("encryption key for SSO settings is missing")
        return cipher.decrypt(model.encrypted_client_secret)["client_secret"]

    async def upsert(
        self,
        *,
        enabled: bool,
        discovery_url: str | None,
        client_id: str | None,
        client_secret: str | None,
        scopes: str,
        admin_groups: tuple[str, ...],
        default_admin: bool,
        team_mapping: dict[str, tuple[TeamGrant, ...]],
        redirect_uri: str | None,
    ) -> SsoSettings:
        model = await self._get_model()
        if model is None:
            model = SsoSettingsModel(id=uuid4())
            self._session.add(model)
        model.enabled = enabled
        model.discovery_url = discovery_url
        model.client_id = client_id
        model.scopes = scopes
        model.admin_groups = list(admin_groups)
        model.default_admin = default_admin
        model.team_mapping = team_mapping_to_json(team_mapping)
        model.redirect_uri = redirect_uri
        if client_secret is not None:
            key_id, cipher = await self._require_keyring().active_credential_cipher()
            model.encrypted_client_secret = cipher.encrypt({"client_secret": client_secret})
            model.key_id = key_id
        # else: keep the existing encrypted_client_secret/key_id untouched.
        await self._session.commit()
        await self._session.refresh(model)
        return model.to_entity()
