"""Application service for provider credentials (platform-admin only).

Stores secret connection values for LLM providers. The repository encrypts the
values at rest (salt key); this service only deals with plaintext dicts and
metadata, never with ciphertext.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from litestar_gateway.application.egress import resolve_allowlisted_addresses
from litestar_gateway.domain.credential_policy import validate_credential_values
from litestar_gateway.domain.egress_policy import EgressAllowlist
from litestar_gateway.domain.entities import Credential, Provider, User
from litestar_gateway.domain.exceptions import (
    CredentialInUse,
    CredentialMisconfigured,
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
    def __init__(
        self,
        repository: CredentialRepository,
        models: ModelRepository,
        egress_allowlist: EgressAllowlist | None = None,
    ) -> None:
        self._repo = repository
        self._models = models
        # Empty unless an operator opted in, which makes `openai_compatible`
        # unusable — the upgrade-safety property (Plan 18 design §4).
        self._egress_allowlist = egress_allowlist or EgressAllowlist(entries=())

    async def _validate_egress(self, provider: Provider, values: dict[str, str]) -> None:
        """For `openai_compatible`, `api_base` must be an operator-authorized
        target. Resolved here so the admin gets an immediate, accurate error;
        re-resolved per call at dispatch (see
        `OpenAICompatibleProviderAdapter._authorize_egress`), which is what
        actually guards against a name drifting out of the allowlisted range
        afterwards."""
        if provider is not Provider.OPENAI_COMPATIBLE:
            return
        api_base = values.get("api_base", "")
        parsed = urlsplit(api_base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise CredentialMisconfigured(
                f"api_base must be an http(s) URL with a host, got {api_base!r}"
            )
        if parsed.username is not None or parsed.password is not None:
            # `ClientKey.endpoint` keeps the endpoint in the clear for metric
            # labels and registry log lines, so a password in the URL would be
            # logged verbatim. Never echo the value back in the message.
            raise CredentialMisconfigured(
                "api_base must not carry userinfo (user:password@host); "
                "put the secret in api_key instead"
            )
        try:
            await resolve_allowlisted_addresses(
                parsed.hostname, parsed.port, self._egress_allowlist
            )
        except ValueError as exc:
            raise CredentialMisconfigured(str(exc)) from exc
        except OSError as exc:
            # Same reason as the MCP path: `getaddrinfo` raises `gaierror`, an
            # OSError, so an unresolvable host was a 500 rather than a 400.
            raise CredentialMisconfigured(
                f"api_base host {parsed.hostname!r} could not be resolved: {exc}"
            ) from exc

    async def create(
        self, actor: User, name: str, provider: Provider, values: dict[str, str]
    ) -> Credential:
        _require_platform_admin(actor)
        validate_credential_values(provider, values)
        await self._validate_egress(provider, values)
        if await self._repo.get_by_name(name) is not None:
            raise CredentialNameExists(name)
        credential = Credential(id=uuid4(), name=name, provider=provider, created_at=_now())
        return await self._repo.add(credential, values)

    async def update(
        self,
        actor: User,
        credential_id: UUID,
        *,
        name: str | None = None,
        values: dict[str, str] | None = None,
    ) -> Credential:
        """Rename a credential and/or replace its secret values (e.g. a rotated
        token). The provider is immutable — recreate to change it. Secrets are
        never revealed, so `values`, when given, is a full replacement set."""
        _require_platform_admin(actor)
        existing = await self._repo.get(credential_id)
        if existing is None:
            raise CredentialNotFound(str(credential_id))
        if values is not None:
            validate_credential_values(existing.provider, values)
            # Re-checked on update too: rotating a credential must not be a way
            # to move its endpoint somewhere the allowlist does not authorize.
            await self._validate_egress(existing.provider, values)
        new_name = name if name is not None and name != existing.name else None
        if new_name is not None:
            clash = await self._repo.get_by_name(new_name)
            if clash is not None and clash.id != credential_id:
                raise CredentialNameExists(new_name)
        if new_name is None and values is None:
            return existing
        return await self._repo.update(credential_id, name=new_name, values=values)

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
