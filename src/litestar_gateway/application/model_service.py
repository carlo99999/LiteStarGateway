"""Application service for model deployments (team-owned or global).

Authorization (platform admin or team admin) is enforced by the caller via
`TeamService.ensure_principal_team_permission`; this service owns the model
invariants: unique name (per team, or across globals), provider == referenced
credential's provider, and the alias-disambiguation rules when a model is
extended to a team that already uses that name.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from litestar_gateway.application.callable_aliases import CallableAliasResolver
from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.entities import Credential, Model, ModelGrant, ModelType, Provider
from litestar_gateway.domain.exceptions import (
    CredentialNotFound,
    ModelNameExists,
    ModelNotFound,
    ProviderMismatch,
)
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.domain.ports import CredentialRepository, ModelRepository

_GLOBAL_SUFFIX = "-global"


def _now() -> datetime:
    return datetime.now(UTC)


def _slug(text: str) -> str:
    """A label safe to embed in a model alias (no spaces/punctuation)."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "team"


@dataclasses.dataclass(frozen=True)
class CallableModel:
    """A model a team can call, with the alias it calls it by and where it came
    from. `origin` is one of `own`, `extended`, `global`."""

    alias: str
    model: Model
    origin: str
    source_team_id: UUID | None


class ModelService:
    def __init__(
        self,
        models: ModelRepository,
        credentials: CredentialRepository,
        callable_resolver: CallableAliasResolver | None = None,
    ) -> None:
        self._models = models
        self._credentials = credentials
        self._callable_resolver = callable_resolver

    async def _validate_credential(self, provider: Provider, credential_id: UUID) -> None:
        credential = await self._credentials.get(credential_id)
        if credential is None:
            raise CredentialNotFound(str(credential_id))
        if credential.provider != provider:
            raise ProviderMismatch(
                f"Model provider '{provider}' does not match credential provider "
                f"'{credential.provider}'"
            )

    async def create(
        self,
        team_id: UUID | None,
        name: str,
        provider: Provider,
        credential_id: UUID,
        model_type: ModelType,
        provider_model_id: str,
        params: dict[str, Any] | None = None,
        params_enforced: dict[str, Any] | None = None,
        max_output_tokens: int | None = None,
        api_version: str | None = None,
        input_cost_per_token: float | None = None,
        output_cost_per_token: float | None = None,
        enabled: bool = True,
    ) -> Model:
        """Create a team-owned model (`team_id` set) or a global one (`team_id`
        None). A team may reuse a name that only exists as a global (it shadows
        the global); it may not reuse one of its own names or an extended alias."""
        if team_id is not None:
            taken = (
                await self._callable_resolver.explicit_taken(team_id, name)
                if self._callable_resolver is not None
                else await self._models.name_taken_in_team(team_id, name)
            )
            if taken:
                raise ModelNameExists(name)
        else:
            taken = (
                await self._callable_resolver.explicit_taken(None, name)
                if self._callable_resolver is not None
                else any(g.name == name for g in await self._models.all_global())
            )
            if taken:
                raise ModelNameExists(name)
        await self._validate_credential(provider, credential_id)
        return await self._models.add(
            Model(
                id=uuid4(),
                team_id=team_id,
                name=name,
                provider=provider,
                credential_id=credential_id,
                type=model_type,
                provider_model_id=provider_model_id,
                params=params or {},
                params_enforced=params_enforced or {},
                max_output_tokens=max_output_tokens,
                api_version=api_version,
                input_cost_per_token=input_cost_per_token,
                output_cost_per_token=output_cost_per_token,
                enabled=enabled,
                created_at=_now(),
                # Remember the owning team so provenance survives a later promote.
                origin_team_id=team_id,
            )
        )

    async def list_for_team(
        self, team_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Model]:
        """The team's OWN models (management view)."""
        return await self._models.list_by_team(team_id, limit=limit, offset=offset)

    async def list_credential_catalog(
        self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Credential]:
        """Return model-binding metadata without exposing credential values."""
        return await self._credentials.list(limit=limit, offset=offset)

    async def list_global(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Model]:
        return await self._models.list_global(limit=limit, offset=offset)

    async def list_callable(self, team_id: UUID) -> list[CallableModel]:
        """Every model a team can call, by effective alias: its own models, the
        models extended to it, and global models (each under its name, or the
        `<name>-global` form when the team already uses that name). Own > extended
        > global on any alias clash."""
        if self._callable_resolver is not None:
            resolved = await self._callable_resolver.list_callable(team_id)
            return [
                CallableModel(
                    item.effective_alias,
                    item.resource,
                    item.binding.origin.value,
                    item.binding.source_team_id,
                )
                for item in resolved
                if item.kind is CallableKind.MODEL and isinstance(item.resource, Model)
            ]

        by_alias: dict[str, CallableModel] = {}

        offset = 0
        while True:
            page = await self._models.list_by_team(team_id, limit=DEFAULT_PAGE_SIZE, offset=offset)
            for model in page:
                by_alias[model.name] = CallableModel(model.name, model, "own", team_id)
            if len(page) < DEFAULT_PAGE_SIZE:
                break
            offset += len(page)

        for grant in await self._models.list_grants_for_team(team_id):
            source = await self._models.get(grant.model_id)
            if source is not None and grant.alias not in by_alias:
                by_alias[grant.alias] = CallableModel(
                    grant.alias, source, "extended", source.team_id
                )

        for model in await self._models.all_global():
            alias = model.name if model.name not in by_alias else f"{model.name}{_GLOBAL_SUFFIX}"
            if alias not in by_alias:
                # Surface the origin team so a promoted global shows its provenance.
                by_alias[alias] = CallableModel(alias, model, "global", model.origin_team_id)

        return sorted(by_alias.values(), key=lambda c: c.alias)

    async def _get_scoped(self, team_id: UUID, model_id: UUID) -> Model:
        model = await self._models.get(model_id)
        if model is None or model.team_id != team_id:
            raise ModelNotFound(str(model_id))
        return model

    async def get_any(self, model_id: UUID) -> Model:
        """Fetch a model by id regardless of owner (platform-admin paths)."""
        model = await self._models.get(model_id)
        if model is None:
            raise ModelNotFound(str(model_id))
        return model

    async def _get_global(self, model_id: UUID) -> Model:
        model = await self._models.get_global(model_id)
        if model is None:
            raise ModelNotFound(str(model_id))
        return model

    async def update(self, team_id: UUID | None, model_id: UUID, **changes: Any) -> Model:
        """Apply the given non-None field changes. `provider`/`credential_id`
        are immutable here; recreate the model to change the provider. Pass
        `team_id=None` to edit a global model (platform-admin path)."""
        model = (
            await self._get_global(model_id)
            if team_id is None
            else await self._get_scoped(team_id, model_id)
        )
        applied = {k: v for k, v in changes.items() if v is not None}
        updated = dataclasses.replace(model, **applied)
        if team_id is not None:
            return await self._models.update(updated)
        persisted = await self._models.update_global(updated)
        if persisted is None:  # deleted or re-scoped after the read
            raise ModelNotFound(str(model_id))
        return persisted

    async def delete(self, team_id: UUID | None, model_id: UUID) -> None:
        if team_id is None:
            if not await self._models.remove_global(model_id):
                raise ModelNotFound(str(model_id))
            return
        model = await self._get_scoped(team_id, model_id)
        await self._models.remove(model.id)

    async def make_global(self, model_id: UUID) -> Model:
        """Promote a team-owned model to a global (platform) one. Its existing
        extension grants become redundant and are removed."""
        model = await self.get_any(model_id)
        if model.team_id is None:
            return model  # already global
        if any(g.name == model.name for g in await self._models.all_global()):
            raise ModelNameExists(model.name)
        return await self._models.promote_to_global(model)

    async def extend(
        self, model_id: UUID, source_label: str, team_ids: list[UUID]
    ) -> list[ModelGrant]:
        """Extend a team-owned model to each target team, returning the grants.
        The alias defaults to the model's name, suffixed with `-<source_label>`
        (and then `-2`, `-3`, …) when the target team already uses that name.
        The owning team and already-granted teams are skipped."""
        model = await self.get_any(model_id)
        label = _slug(source_label)
        grants: list[ModelGrant] = []
        existing = {g.team_id for g in await self._models.list_grants_for_model(model_id)}
        for team_id in team_ids:
            if team_id == model.team_id or team_id in existing:
                continue
            alias = await self._disambiguate(team_id, model.name, label)
            grants.append(
                ModelGrant(
                    id=uuid4(),
                    model_id=model_id,
                    team_id=team_id,
                    alias=alias,
                    created_at=_now(),
                )
            )
        return await self._models.add_grants(grants)

    async def _disambiguate(self, team_id: UUID, base: str, source_label: str) -> str:
        async def taken(alias: str) -> bool:
            if self._callable_resolver is not None:
                return await self._callable_resolver.slot_reserved(team_id, alias)
            return await self._models.name_taken_in_team(team_id, alias)

        if not await taken(base):
            return base
        candidate = f"{base}-{source_label}"
        suffix = 2
        while await taken(candidate):
            candidate = f"{base}-{source_label}-{suffix}"
            suffix += 1
        return candidate

    async def list_grants(self, model_id: UUID) -> list[ModelGrant]:
        return await self._models.list_grants_for_model(model_id)

    async def unextend(self, grant_id: UUID) -> None:
        await self._models.remove_grant(grant_id)
