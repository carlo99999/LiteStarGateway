"""Port — provider credential persistence."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from litestar_gateway.domain.entities import Credential
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE


class CredentialRepository(Protocol):
    """Persistence port for provider credentials.

    The adapter encrypts `values` at rest; metadata reads never expose secrets.
    """

    async def add(self, credential: Credential, values: dict[str, str]) -> Credential: ...

    async def get(self, credential_id: UUID) -> Credential | None: ...

    async def get_by_name(self, name: str) -> Credential | None: ...

    async def list(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Credential]: ...

    async def get_values(self, credential_id: UUID) -> dict[str, str] | None: ...

    async def remove(self, credential_id: UUID) -> None: ...
