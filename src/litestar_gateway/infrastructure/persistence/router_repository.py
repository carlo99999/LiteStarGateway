"""SQLAlchemy adapters for the `RouterRepository` and `RoutingDecisionLog` ports."""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.exceptions import (
    CredentialMisconfigured,
    InvalidRouterConfig,
    RouterGrantNotFound,
    RouterNameExists,
    RouterNotFound,
    RouterRevisionConflict,
    RouterShared,
    SaltKeyMissing,
)
from litestar_gateway.domain.routing import (
    BEARER_TOKEN_MASK,
    RouterConfig,
    RouterGrant,
    RoutingDecisionRecord,
)
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.callable_alias_slots import (
    claim_direct,
    lock_resource_lifecycle,
    promote_direct,
    rename_direct,
    tombstone_grant,
    tombstone_resource,
)
from litestar_gateway.infrastructure.persistence.orm import (
    CallableAliasRecord,
    RouterGrantModel,
    RouterModel,
    RouterRevisionModel,
    RoutingDecisionModel,
)

_GLOBAL_SUFFIX = "-global"


def _candidates_json(router: RouterConfig) -> list[dict]:
    rows: list[dict] = []
    for candidate in router.candidates:
        row: dict[str, Any] = dict(dataclasses.asdict(candidate))
        if candidate.model_id is None:
            raise InvalidRouterConfig(
                f"Candidate '{candidate.model_name}' is missing its stable model identity"
            )
        row["model_id"] = str(candidate.model_id)
        if candidate.source_team_id is not None:
            row["source_team_id"] = str(candidate.source_team_id)
        rows.append(row)
    return rows


# The webhook strategy's `bearer_token` is a secret. At rest it is replaced by
# an **envelope**: the token encrypted with a keyring credential data key, plus
# the id of the key that produced it (same scheme as `CredentialRepository`).
# A plain string is a legacy plaintext row and is passed through on read; it is
# upgraded to an envelope the next time the router is written.
_TOKEN_KEY = "bearer_token"
_ENVELOPE_KEYS = frozenset({"key_id", "token"})


def _is_envelope(value: object) -> bool:
    return isinstance(value, dict) and set(value) == _ENVELOPE_KEYS


class SQLAlchemyRouterRepository:
    def __init__(self, session: AsyncSession, keyring: Keyring | None = None) -> None:
        # `keyring` encrypts/decrypts the webhook `bearer_token` inside
        # `strategy_config`; it is only needed when such a token is present.
        self._session = session
        self._keyring = keyring

    def _require_keyring(self) -> Keyring:
        if self._keyring is None:
            raise SaltKeyMissing("SALT_KEY is not configured")
        return self._keyring

    # --- bearer_token envelope encryption (top level and under "shadow") ---

    async def _encrypt_section(self, section: dict) -> dict:
        token = section.get(_TOKEN_KEY)
        if not isinstance(token, str):
            return section  # absent, or already an envelope (preserved on update)
        key_id, cipher = await self._require_keyring().active_credential_cipher()
        envelope = {"key_id": str(key_id), "token": cipher.encrypt({_TOKEN_KEY: token})}
        return {**section, _TOKEN_KEY: envelope}

    async def _decrypt_section(self, section: dict) -> dict:
        token = section.get(_TOKEN_KEY)
        if not _is_envelope(token):
            return section  # absent, or a legacy plaintext row: pass through
        assert isinstance(token, dict)  # narrowed by _is_envelope
        cipher = await self._require_keyring().credential_cipher_for(UUID(token["key_id"]))
        if cipher is None:  # pragma: no cover - a missing key row is not expected
            raise CredentialMisconfigured("encryption key for webhook bearer token is missing")
        return {**section, _TOKEN_KEY: cipher.decrypt(token["token"])[_TOKEN_KEY]}

    @staticmethod
    def _preserve_section(section: dict, stored: object) -> dict:
        """An update that echoes the mask keeps the token already stored."""
        if section.get(_TOKEN_KEY) != BEARER_TOKEN_MASK:
            return section
        stored_token = stored.get(_TOKEN_KEY) if isinstance(stored, dict) else None
        if stored_token is None:
            return section
        return {**section, _TOKEN_KEY: stored_token}

    async def _map_config(self, config: dict, transform) -> dict:
        result = await transform(config)
        shadow = result.get("shadow")
        if isinstance(shadow, dict):
            new_shadow = await transform(shadow)
            if new_shadow is not shadow:
                result = {**result, "shadow": new_shadow}
        return result

    def _preserve_masked_tokens(self, config: dict, stored: dict) -> dict:
        result = self._preserve_section(config, stored)
        shadow = result.get("shadow")
        if isinstance(shadow, dict):
            new_shadow = self._preserve_section(shadow, stored.get("shadow"))
            if new_shadow is not shadow:
                result = {**result, "shadow": new_shadow}
        return result

    async def _to_entity(
        self,
        model: RouterModel,
        revision: RouterRevisionModel | None = None,
        grant: RouterGrantModel | None = None,
    ) -> RouterConfig:
        if revision is None and model.current_revision_id is not None:
            revision = await self._session.get(RouterRevisionModel, model.current_revision_id)
        entity = (
            revision.to_entity(
                model,
                grant_id=grant.id if grant is not None else None,
                ack_active_prompt_egress=(
                    grant.ack_active_prompt_egress if grant is not None else False
                ),
                ack_shadow_prompt_egress=(
                    grant.ack_shadow_prompt_egress if grant is not None else False
                ),
            )
            if revision is not None
            else model.to_entity()
        )
        config = await self._map_config(entity.strategy_config, self._decrypt_section)
        if config is entity.strategy_config:
            return entity
        return dataclasses.replace(entity, strategy_config=config)

    async def _new_revision(
        self,
        model: RouterModel,
        router: RouterConfig,
        revision_number: int,
        *,
        stored_config: dict | None = None,
    ) -> RouterRevisionModel:
        if router.default_model_id is None:
            raise InvalidRouterConfig("default_model is missing its stable model identity")
        config = router.strategy_config
        if stored_config is not None:
            config = self._preserve_masked_tokens(config, stored_config)
        revision = RouterRevisionModel(
            id=uuid4(),
            router_id=model.id,
            revision_number=revision_number,
            candidates=_candidates_json(router),
            default_model_id=router.default_model_id,
            default_model_name=router.default_model,
            strategy=router.strategy,
            strategy_config=await self._map_config(config, self._encrypt_section),
            shadow_strategy=router.shadow_strategy,
            enabled=router.enabled,
        )
        self._session.add(revision)
        await self._session.flush()
        return revision

    @staticmethod
    def _sync_projection(model: RouterModel, revision: RouterRevisionModel) -> None:
        """Keep the legacy columns as an exact projection of the current head."""
        model.candidates = revision.candidates
        model.default_model = revision.default_model_name
        model.strategy = revision.strategy
        model.strategy_config = revision.strategy_config
        model.shadow_strategy = revision.shadow_strategy
        model.enabled = revision.enabled
        model.current_revision_id = revision.id

    # --- CRUD ---

    async def add(self, router: RouterConfig) -> RouterConfig:
        model = RouterModel(
            id=router.id,
            team_id=router.team_id,
            name=router.name,
            candidates=_candidates_json(router),
            default_model=router.default_model,
            strategy=router.strategy,
            strategy_config={},
            shadow_strategy=router.shadow_strategy,
            enabled=router.enabled,
            origin_team_id=router.origin_team_id,
        )
        try:
            self._session.add(model)
            await self._session.flush()
            revision = await self._new_revision(model, router, 1)
            self._sync_projection(model, revision)
            await claim_direct(
                self._session, CallableKind.ROUTER, router.id, router.team_id, router.name
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(router.name) from exc
        await self._session.refresh(model)
        await self._session.refresh(revision)
        return await self._to_entity(model, revision)

    async def get(self, team_id: UUID, router_id: UUID) -> RouterConfig | None:
        model = await self._session.get(RouterModel, router_id)
        if model is None or model.team_id != team_id:
            return None
        return await self._to_entity(model)

    async def get_any(self, router_id: UUID) -> RouterConfig | None:
        """Fetch a router by id regardless of owner (platform-admin paths)."""
        model = await self._session.get(RouterModel, router_id)
        return await self._to_entity(model) if model else None

    async def get_revision(self, router_id: UUID, revision_id: UUID) -> RouterConfig | None:
        model = await self._session.get(RouterModel, router_id)
        revision = await self._session.get(RouterRevisionModel, revision_id)
        if model is None or revision is None or revision.router_id != router_id:
            return None
        return await self._to_entity(model, revision)

    async def get_for_grant(
        self, grant_id: UUID, team_id: UUID | None = None
    ) -> RouterConfig | None:
        grant = await self._session.get(RouterGrantModel, grant_id)
        if grant is None or (team_id is not None and grant.team_id != team_id):
            return None
        model = await self._session.get(RouterModel, grant.router_id)
        revision = (
            await self._session.get(RouterRevisionModel, grant.revision_id)
            if grant.revision_id is not None
            else None
        )
        if model is None or revision is None or revision.router_id != model.id:
            return None
        return await self._to_entity(model, revision, grant)

    async def list_revisions(self, router_id: UUID) -> list[RouterConfig]:
        model = await self._session.get(RouterModel, router_id)
        if model is None:
            return []
        revisions = await self._session.scalars(
            select(RouterRevisionModel)
            .where(RouterRevisionModel.router_id == router_id)
            .order_by(RouterRevisionModel.revision_number.desc())
        )
        return [await self._to_entity(model, revision) for revision in revisions]

    async def get_by_name(self, team_id: UUID, name: str) -> RouterConfig | None:
        # Priority mirrors models: own → extended (grant alias) → global by name →
        # `<base>-global` when the team's own `<base>` shadows a global.
        own = await self._session.scalar(
            select(RouterModel).where(RouterModel.team_id == team_id, RouterModel.name == name)
        )
        if own is not None:
            return await self._to_entity(own)
        grant = await self._session.scalar(
            select(RouterGrantModel).where(
                RouterGrantModel.team_id == team_id, RouterGrantModel.alias == name
            )
        )
        if grant is not None:
            return await self.get_for_grant(grant.id, team_id)
        glob = await self._session.scalar(
            select(RouterModel).where(RouterModel.team_id.is_(None), RouterModel.name == name)
        )
        if glob is not None:
            return await self._to_entity(glob)
        if name.endswith(_GLOBAL_SUFFIX):
            base = name[: -len(_GLOBAL_SUFFIX)]
            shadows = await self._session.scalar(
                select(RouterModel.id)
                .where(RouterModel.team_id == team_id, RouterModel.name == base)
                .limit(1)
            )
            if shadows is not None:
                glob = await self._session.scalar(
                    select(RouterModel).where(
                        RouterModel.team_id.is_(None), RouterModel.name == base
                    )
                )
                if glob is not None:
                    return await self._to_entity(glob)
        return None

    async def name_taken_in_team(self, team_id: UUID, name: str) -> bool:
        own = await self._session.scalar(
            select(RouterModel.id)
            .where(RouterModel.team_id == team_id, RouterModel.name == name)
            .limit(1)
        )
        if own is not None:
            return True
        grant = await self._session.scalar(
            select(RouterGrantModel.id)
            .where(RouterGrantModel.team_id == team_id, RouterGrantModel.alias == name)
            .limit(1)
        )
        return grant is not None

    async def list_by_team(self, team_id: UUID) -> list[RouterConfig]:
        result = await self._session.scalars(
            select(RouterModel).where(RouterModel.team_id == team_id).order_by(RouterModel.name)
        )
        return [await self._to_entity(model) for model in result]

    async def list_global(self) -> list[RouterConfig]:
        result = await self._session.scalars(
            select(RouterModel).where(RouterModel.team_id.is_(None)).order_by(RouterModel.name)
        )
        return [await self._to_entity(model) for model in result]

    async def all_global(self) -> list[RouterConfig]:
        return await self.list_global()

    async def update(self, router: RouterConfig) -> RouterConfig:
        model = await lock_resource_lifecycle(self._session, CallableKind.ROUTER, router.id)
        # Scope to the owner (team routers) — `None == None` allows global→global
        # edits; ownership changes go through `promote_to_global`, not here.
        if not isinstance(model, RouterModel) or model.team_id != router.team_id:
            raise RouterNotFound(str(router.id))
        if model.current_revision_id is None:
            raise RouterRevisionConflict("router has no active revision")
        if router.revision_id is not None and router.revision_id != model.current_revision_id:
            raise RouterRevisionConflict(
                f"router head changed: expected {router.revision_id}, "
                f"found {model.current_revision_id}"
            )
        head = await self._session.get(RouterRevisionModel, model.current_revision_id)
        if head is None:
            raise RouterRevisionConflict("router head revision is missing")
        old_name = model.name
        try:
            model.origin_team_id = router.origin_team_id
            model.name = router.name
            revision = await self._new_revision(
                model,
                router,
                head.revision_number + 1,
                stored_config=head.strategy_config,
            )
            self._sync_projection(model, revision)
            if old_name != router.name:
                await rename_direct(
                    self._session,
                    CallableKind.ROUTER,
                    router.id,
                    router.team_id,
                    old_name,
                    router.name,
                )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(router.name) from exc
        await self._session.refresh(model)
        await self._session.refresh(revision)
        return await self._to_entity(model, revision)

    async def delete(self, team_id: UUID, router_id: UUID) -> bool:
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        model = await lock_resource_lifecycle(self._session, CallableKind.ROUTER, router_id)
        if not isinstance(model, RouterModel) or model.team_id != team_id:
            return False
        if await self._session.scalar(
            select(RouterGrantModel.id).where(RouterGrantModel.router_id == router_id).limit(1)
        ):
            raise RouterShared("revoke every router grant before deleting the source router")
        await tombstone_resource(self._session, CallableKind.ROUTER, router_id)
        result: Any = await self._session.execute(
            delete(RouterModel).where(RouterModel.id == router_id, RouterModel.team_id == team_id)
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def delete_global(self, router_id: UUID) -> bool:
        model = await lock_resource_lifecycle(self._session, CallableKind.ROUTER, router_id)
        if not isinstance(model, RouterModel) or model.team_id is not None:
            return False
        await tombstone_resource(self._session, CallableKind.ROUTER, router_id)
        result: Any = await self._session.execute(
            delete(RouterModel).where(RouterModel.id == router_id, RouterModel.team_id.is_(None))
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def promote_to_global(self, router_id: UUID) -> RouterConfig | None:
        """Reassign a team-owned router to the platform (team_id → None),
        keeping its origin team for provenance. Removes its extension grants."""
        model = await lock_resource_lifecycle(self._session, CallableKind.ROUTER, router_id)
        if not isinstance(model, RouterModel):
            return None
        try:
            if model.team_id is not None:
                if await self._session.scalar(
                    select(RouterGrantModel.id)
                    .where(RouterGrantModel.router_id == router_id)
                    .limit(1)
                ):
                    raise RouterShared(
                        "revoke every router grant before promoting the source router"
                    )
                model.origin_team_id = model.team_id
                model.team_id = None
                await promote_direct(self._session, CallableKind.ROUTER, router_id, model.name)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(model.name) from exc
        await self._session.refresh(model)
        return await self._to_entity(model)

    # Grants (extending a team router to other teams).

    async def add_grant(self, grant: RouterGrant) -> RouterGrant:
        return (await self.add_grants([grant]))[0]

    async def add_grants(self, grants: list[RouterGrant]) -> list[RouterGrant]:
        if not grants:
            return []
        resource_ids = {grant.router_id for grant in grants}
        if len(resource_ids) != 1:
            raise ValueError("one add_grants call must target exactly one router")
        router_id = next(iter(resource_ids))
        source = await lock_resource_lifecycle(self._session, CallableKind.ROUTER, router_id)
        if not isinstance(source, RouterModel) or source.team_id is None:
            raise RouterNameExists(grants[0].alias)
        if source.current_revision_id is None:
            raise RouterRevisionConflict("source router has no active revision")
        if any(
            grant.revision_id is not None and grant.revision_id != source.current_revision_id
            for grant in grants
        ):
            raise RouterRevisionConflict("extension must pin the current router revision")
        revision_id = source.current_revision_id
        records = [
            RouterGrantModel(
                id=grant.id,
                router_id=grant.router_id,
                team_id=grant.team_id,
                alias=grant.alias,
                revision_id=revision_id,
                ack_active_prompt_egress=grant.ack_active_prompt_egress,
                ack_shadow_prompt_egress=grant.ack_shadow_prompt_egress,
            )
            for grant in grants
        ]
        try:
            self._session.add_all(records)
            await self._session.flush()
            self._session.add_all(
                [
                    CallableAliasRecord(
                        id=uuid4(),
                        team_id=grant.team_id,
                        alias=grant.alias,
                        router_id=grant.router_id,
                        router_grant_id=grant.id,
                    )
                    for grant in grants
                ]
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(grants[0].alias) from exc
        for record in records:
            await self._session.refresh(record)
        revision = await self._session.get(RouterRevisionModel, revision_id)
        return [
            record.to_entity(revision.revision_number if revision is not None else None)
            for record in records
        ]

    async def get_grant(self, grant_id: UUID) -> RouterGrant | None:
        record = await self._session.get(RouterGrantModel, grant_id)
        if record is None:
            return None
        revision = (
            await self._session.get(RouterRevisionModel, record.revision_id)
            if record.revision_id is not None
            else None
        )
        return record.to_entity(revision.revision_number if revision is not None else None)

    async def stage_grant_upgrade(
        self,
        grant_id: UUID,
        target_revision_id: UUID,
        expected_revision_id: UUID,
        *,
        ack_active_prompt_egress: bool,
        ack_shadow_prompt_egress: bool,
    ) -> RouterGrant:
        if self._session.get_bind().dialect.name == "sqlite":
            # Acquire SQLite's writer lock before the compare-and-swap read.
            await self._session.execute(
                update(RouterGrantModel)
                .where(RouterGrantModel.id == grant_id)
                .values(alias=RouterGrantModel.alias)
            )
        statement = select(RouterGrantModel).where(RouterGrantModel.id == grant_id)
        if self._session.get_bind().dialect.name != "sqlite":
            statement = statement.with_for_update()
        grant = await self._session.scalar(statement)
        if grant is None:
            raise RouterGrantNotFound(str(grant_id))
        if grant.revision_id != expected_revision_id:
            raise RouterRevisionConflict(
                f"grant revision changed: expected {expected_revision_id}, "
                f"found {grant.revision_id}"
            )
        revision = await self._session.get(RouterRevisionModel, target_revision_id)
        if revision is None or revision.router_id != grant.router_id:
            raise RouterRevisionConflict("target revision does not belong to the granted router")
        grant.revision_id = revision.id
        grant.ack_active_prompt_egress = ack_active_prompt_egress
        grant.ack_shadow_prompt_egress = ack_shadow_prompt_egress
        await self._session.flush()
        return grant.to_entity(revision.revision_number)

    async def remove_grant(self, grant_id: UUID) -> None:
        grant = await self._session.scalar(
            select(RouterGrantModel).where(RouterGrantModel.id == grant_id)
        )
        if grant is None:
            return
        source = await lock_resource_lifecycle(self._session, CallableKind.ROUTER, grant.router_id)
        if not isinstance(source, RouterModel):
            return
        if await self._session.get(RouterGrantModel, grant_id, populate_existing=True) is None:
            return
        await tombstone_grant(self._session, CallableKind.ROUTER, grant_id)
        await self._session.execute(delete(RouterGrantModel).where(RouterGrantModel.id == grant_id))
        await self._session.commit()

    async def list_grants_for_router(self, router_id: UUID) -> list[RouterGrant]:
        records = await self._session.scalars(
            select(RouterGrantModel)
            .where(RouterGrantModel.router_id == router_id)
            .order_by(RouterGrantModel.created_at, RouterGrantModel.id)
        )
        return [await self._grant_entity(record) for record in records]

    async def list_grants_for_team(self, team_id: UUID) -> list[RouterGrant]:
        records = await self._session.scalars(
            select(RouterGrantModel)
            .where(RouterGrantModel.team_id == team_id)
            .order_by(RouterGrantModel.created_at, RouterGrantModel.id)
        )
        return [await self._grant_entity(record) for record in records]

    async def _grant_entity(self, record: RouterGrantModel) -> RouterGrant:
        revision = (
            await self._session.get(RouterRevisionModel, record.revision_id)
            if record.revision_id is not None
            else None
        )
        return record.to_entity(revision.revision_number if revision is not None else None)


class SQLAlchemyRoutingDecisionLog:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, decision: RoutingDecisionRecord) -> None:
        self._session.add(
            RoutingDecisionModel(
                id=decision.id,
                team_id=decision.team_id,
                router_id=decision.router_id,
                router_name=decision.router_name,
                strategy=decision.strategy,
                chosen_model=decision.chosen_model,
                tier=decision.tier,
                score=decision.score,
                signals=list(decision.signals),
                decision_ms=decision.decision_ms,
                is_shadow=decision.is_shadow,
                fallback_used=decision.fallback_used,
                api_key_id=decision.api_key_id,
                chosen_input_cost=decision.chosen_input_cost,
                chosen_output_cost=decision.chosen_output_cost,
                alt_input_cost=decision.alt_input_cost,
                alt_output_cost=decision.alt_output_cost,
                prompt_tokens=decision.prompt_tokens,
                completion_tokens=decision.completion_tokens,
                user_text=decision.user_text,
                system_prompt=decision.system_prompt,
                router_revision_id=decision.router_revision_id,
                chosen_model_id=decision.chosen_model_id,
            )
        )
        await self._session.commit()

    async def update_usage(
        self, decision_id: UUID, prompt_tokens: int, completion_tokens: int
    ) -> None:
        await self._session.execute(
            update(RoutingDecisionModel)
            .where(RoutingDecisionModel.id == decision_id)
            .values(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        )
        await self._session.commit()

    async def list_decisions(
        self,
        team_id: UUID,
        router_id: UUID,
        *,
        strategy: str | None = None,
        chosen_model: str | None = None,
        is_shadow: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RoutingDecisionRecord]:
        stmt = select(RoutingDecisionModel).where(
            RoutingDecisionModel.team_id == team_id,
            RoutingDecisionModel.router_id == router_id,
        )
        if strategy is not None:
            stmt = stmt.where(RoutingDecisionModel.strategy == strategy)
        if chosen_model is not None:
            stmt = stmt.where(RoutingDecisionModel.chosen_model == chosen_model)
        if is_shadow is not None:
            stmt = stmt.where(RoutingDecisionModel.is_shadow == is_shadow)
        stmt = (
            stmt.order_by(RoutingDecisionModel.created_at.desc(), RoutingDecisionModel.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return [model.to_entity() for model in await self._session.scalars(stmt)]

    async def distribution(
        self, team_id: UUID, router_id: UUID
    ) -> list[tuple[str, str | None, bool, int]]:
        stmt = (
            select(
                RoutingDecisionModel.chosen_model,
                RoutingDecisionModel.tier,
                RoutingDecisionModel.is_shadow,
                func.count(),
            )
            .where(
                RoutingDecisionModel.team_id == team_id,
                RoutingDecisionModel.router_id == router_id,
            )
            .group_by(
                RoutingDecisionModel.chosen_model,
                RoutingDecisionModel.tier,
                RoutingDecisionModel.is_shadow,
            )
        )
        return [tuple(row) for row in await self._session.execute(stmt)]

    async def savings(self, team_id: UUID, router_id: UUID) -> tuple[float, int, int]:
        return await self._savings_aggregate(
            RoutingDecisionModel.team_id == team_id,
            RoutingDecisionModel.router_id == router_id,
            RoutingDecisionModel.is_shadow.is_(False),
        )

    async def platform_savings(self) -> tuple[float, int, int]:
        # Every team and router — the dashboard's platform-wide figure.
        return await self._savings_aggregate(RoutingDecisionModel.is_shadow.is_(False))

    async def team_savings(self, team_id: UUID) -> tuple[float, int, int]:
        # One team, all of its routers.
        return await self._savings_aggregate(
            RoutingDecisionModel.team_id == team_id,
            RoutingDecisionModel.is_shadow.is_(False),
        )

    async def _savings_aggregate(self, *base: Any) -> tuple[float, int, int]:
        # One point-in-time query (a single row snapshot, so the three figures
        # can't drift under concurrent inserts) for: Σ savings over *priced*
        # decisions, the priced count, and the total count. "Priced" = actual
        # token usage AND both cost profiles present; savings = (alt − chosen)
        # unit cost × the request's actual tokens.
        priced = and_(
            RoutingDecisionModel.prompt_tokens.is_not(None),
            RoutingDecisionModel.completion_tokens.is_not(None),
            RoutingDecisionModel.alt_input_cost.is_not(None),
            RoutingDecisionModel.chosen_input_cost.is_not(None),
            RoutingDecisionModel.alt_output_cost.is_not(None),
            RoutingDecisionModel.chosen_output_cost.is_not(None),
        )
        savings_expr = (
            RoutingDecisionModel.alt_input_cost - RoutingDecisionModel.chosen_input_cost
        ) * RoutingDecisionModel.prompt_tokens + (
            RoutingDecisionModel.alt_output_cost - RoutingDecisionModel.chosen_output_cost
        ) * RoutingDecisionModel.completion_tokens
        total, counted_n, all_n = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(case((priced, savings_expr), else_=0.0)), 0.0),
                    func.coalesce(func.sum(case((priced, 1), else_=0)), 0),
                    func.count(),
                ).where(*base)
            )
        ).one()
        return float(total or 0.0), int(counted_n or 0), int((all_n or 0) - (counted_n or 0))
