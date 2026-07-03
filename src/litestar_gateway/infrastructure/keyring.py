"""Keyring: envelope-encryption operations over the DB-stored rotating keys.

Keys are read from the DB per operation (no cache to invalidate) and unwrapped
with a fixed **master** cipher. Masters are per purpose so their concerns stay
independent: credentials are wrapped by `SALT_KEY`, JWT signing keys by
`JWT_SECRET`. Masters never rotate; the data keys they wrap do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from litestar_test.domain.entities import KeyPurpose, SecretKey
from litestar_test.domain.ports import SecretKeyRepository
from litestar_test.infrastructure.crypto import (
    DataCipher,
    MasterCipher,
    build_master_cipher,
    new_key_material,
)

# A JWT signing key stays active until the next daily rotation, so a token signed
# just before then is still valid for the full token TTL afterwards. Keep keys an
# extra rotation interval beyond the TTL before deleting, or we'd invalidate tokens
# that are still within their lifetime (spurious mid-session logouts).
_JWT_KEY_RETENTION_GRACE = timedelta(days=1)


def _now() -> datetime:
    return datetime.now(UTC)


class Keyring:
    def __init__(
        self,
        keys: SecretKeyRepository,
        credential_master_key: str | None,
        jwt_master_key: str,
    ) -> None:
        self._keys = keys
        self._credential_master_key = credential_master_key
        self._jwt_master_key = jwt_master_key

    def _master(self, purpose: KeyPurpose) -> MasterCipher:
        if purpose is KeyPurpose.CREDENTIAL:
            return build_master_cipher(self._credential_master_key)  # raises if SALT_KEY unset
        return MasterCipher(self._jwt_master_key)

    async def _create(self, purpose: KeyPurpose) -> SecretKey:
        return await self._keys.add(
            SecretKey(
                id=uuid4(),
                purpose=purpose,
                material=self._master(purpose).wrap(new_key_material()),
                created_at=_now(),
                retired_at=None,
            )
        )

    async def ensure_active(self, purpose: KeyPurpose) -> SecretKey:
        """Return the active key for `purpose`, creating the first one if none."""
        return await self._keys.get_active(purpose) or await self._create(purpose)

    # --- credential encryption ---

    async def active_credential_cipher(self) -> tuple[UUID, DataCipher]:
        key = await self.ensure_active(KeyPurpose.CREDENTIAL)
        return key.id, DataCipher(self._master(KeyPurpose.CREDENTIAL).unwrap(key.material))

    async def credential_cipher_for(self, key_id: UUID) -> DataCipher | None:
        key = await self._keys.get(key_id)
        if key is None:
            return None
        return DataCipher(self._master(KeyPurpose.CREDENTIAL).unwrap(key.material))

    # --- jwt signing/verification ---

    async def active_jwt_secret(self) -> str:
        key = await self.ensure_active(KeyPurpose.JWT)
        return self._master(KeyPurpose.JWT).unwrap(key.material).decode("utf-8")

    async def jwt_verification_secrets(self) -> list[str]:
        master = self._master(KeyPurpose.JWT)
        keys = await self._keys.list_usable(KeyPurpose.JWT)
        return [master.unwrap(k.material).decode("utf-8") for k in keys]

    # --- rotation ---

    async def rotate_jwt(self, max_age: timedelta) -> None:
        """Add a fresh JWT signing key and drop keys too old to have signed any
        still-valid token (older than the access-token TTL)."""
        await self._create(KeyPurpose.JWT)
        cutoff = _now() - max_age - _JWT_KEY_RETENTION_GRACE
        for key in await self._keys.list_usable(KeyPurpose.JWT):
            if key.created_at < cutoff:
                await self._keys.delete(key.id)

    async def new_credential_key(self) -> SecretKey:
        """Add a fresh, active credential data key (does not re-encrypt)."""
        return await self._create(KeyPurpose.CREDENTIAL)

    async def retire_old_credential_keys(self) -> None:
        """Retire every credential key except the newest (the active one). Retired
        keys stay readable, so anything not yet re-encrypted can still decrypt."""
        usable = await self._keys.list_usable(KeyPurpose.CREDENTIAL)  # newest first
        for key in usable[1:]:
            await self._keys.retire(key.id, _now())
