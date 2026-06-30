"""Application service for provider credentials (platform-admin only).

Stores secret connection values for LLM providers. The repository encrypts the
values at rest (salt key); this service only deals with plaintext dicts and
metadata, never with ciphertext.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_test.domain.entities import Credential, Provider, User
from litestar_test.domain.exceptions import (
    CredentialNameExists,
    CredentialNotFound,
    PermissionDenied,
)
from litestar_test.domain.ports import CredentialRepository


def _now() -> datetime:
    return datetime.now(UTC)


def _require_platform_admin(actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Platform admin privileges required")


class CredentialService:
    def __init__(self, repository: CredentialRepository) -> None:
        self._repo = repository

    async def create(
        self, actor: User, name: str, provider: Provider, values: dict[str, str]
    ) -> Credential:
        _require_platform_admin(actor)
        if await self._repo.get_by_name(name) is not None:
            raise CredentialNameExists(name)
        credential = Credential(id=uuid4(), name=name, provider=provider, created_at=_now())
        return await self._repo.add(credential, values)

    async def list(self, actor: User) -> list[Credential]:
        _require_platform_admin(actor)
        return await self._repo.list()

    async def delete(self, actor: User, credential_id: UUID) -> None:
        _require_platform_admin(actor)
        if await self._repo.get(credential_id) is None:
            raise CredentialNotFound(str(credential_id))
        await self._repo.remove(credential_id)

    async def reveal_values(self, actor: User, credential_id: UUID) -> dict[str, str]:
        """Decrypt the stored values. Internal use (e.g. calling the provider)."""
        _require_platform_admin(actor)
        values = await self._repo.get_values(credential_id)
        if values is None:
            raise CredentialNotFound(str(credential_id))
        return values
