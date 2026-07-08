"""Application service for provider credentials (platform-admin only).

Stores secret connection values for LLM providers. The repository encrypts the
values at rest (salt key); this service only deals with plaintext dicts and
metadata, never with ciphertext.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.domain.credential_policy import validate_credential_values
from litestar_gateway.domain.entities import Credential, Provider, User
from litestar_gateway.domain.exceptions import (
    CredentialInUse,
    CredentialNameExists,
    CredentialNotFound,
    PermissionDenied,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import CredentialRepository, ModelRepository


def _now() -> datetime:
    return datetime.now(UTC)


def _require_platform_admin(actor: User) -> None:
    if not actor.is_admin:
        raise PermissionDenied("Platform admin privileges required")


class CredentialService:
    def __init__(self, repository: CredentialRepository, models: ModelRepository) -> None:
        self._repo = repository
        self._models = models

    async def create(
        self, actor: User, name: str, provider: Provider, values: dict[str, str]
    ) -> Credential:
        _require_platform_admin(actor)
        validate_credential_values(provider, values)
        if await self._repo.get_by_name(name) is not None:
            raise CredentialNameExists(name)
        credential = Credential(id=uuid4(), name=name, provider=provider, created_at=_now())
        return await self._repo.add(credential, values)

    async def list(
        self, actor: User, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Credential]:
        _require_platform_admin(actor)
        return await self._repo.list(limit=limit, offset=offset)

    async def delete(self, actor: User, credential_id: UUID) -> None:
        _require_platform_admin(actor)
        if await self._repo.get(credential_id) is None:
            raise CredentialNotFound(str(credential_id))
        # Guard the FK: deleting an in-use credential would raise IntegrityError on
        # Postgres and silently orphan models on SQLite. Reject it as a 409 instead.
        if await self._models.exists_for_credential(credential_id):
            raise CredentialInUse(str(credential_id))
        await self._repo.remove(credential_id)

    async def reveal_values(self, actor: User, credential_id: UUID) -> dict[str, str]:
        """Decrypt the stored values. Internal use (e.g. calling the provider)."""
        _require_platform_admin(actor)
        values = await self._repo.get_values(credential_id)
        if values is None:
            raise CredentialNotFound(str(credential_id))
        return values
