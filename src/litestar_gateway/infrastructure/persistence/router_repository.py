"""SQLAlchemy adapters for the `RouterRepository` and `RoutingDecisionLog` ports."""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.exceptions import (
    CredentialMisconfigured,
    RouterNameExists,
    RouterNotFound,
    SaltKeyMissing,
)
from litestar_gateway.domain.routing import BEARER_TOKEN_MASK, RouterConfig, RoutingDecisionRecord
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.orm import RouterModel, RoutingDecisionModel


def _candidates_json(router: RouterConfig) -> list[dict]:
    return [dataclasses.asdict(candidate) for candidate in router.candidates]


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

    async def _to_entity(self, model: RouterModel) -> RouterConfig:
        entity = model.to_entity()
        config = await self._map_config(entity.strategy_config, self._decrypt_section)
        if config is entity.strategy_config:
            return entity
        return dataclasses.replace(entity, strategy_config=config)

    # --- CRUD ---

    async def add(self, router: RouterConfig) -> RouterConfig:
        model = RouterModel(
            id=router.id,
            team_id=router.team_id,
            name=router.name,
            candidates=_candidates_json(router),
            default_model=router.default_model,
            strategy=router.strategy,
            strategy_config=await self._map_config(router.strategy_config, self._encrypt_section),
            shadow_strategy=router.shadow_strategy,
            enabled=router.enabled,
        )
        self._session.add(model)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(router.name) from exc
        await self._session.refresh(model)
        return await self._to_entity(model)

    async def get(self, team_id: UUID, router_id: UUID) -> RouterConfig | None:
        model = await self._session.get(RouterModel, router_id)
        if model is None or model.team_id != team_id:
            return None
        return await self._to_entity(model)

    async def get_by_name(self, team_id: UUID, name: str) -> RouterConfig | None:
        model = await self._session.scalar(
            select(RouterModel).where(RouterModel.team_id == team_id, RouterModel.name == name)
        )
        return await self._to_entity(model) if model else None

    async def list_by_team(self, team_id: UUID) -> list[RouterConfig]:
        result = await self._session.scalars(
            select(RouterModel).where(RouterModel.team_id == team_id).order_by(RouterModel.name)
        )
        return [await self._to_entity(model) for model in result]

    async def update(self, router: RouterConfig) -> RouterConfig:
        model = await self._session.get(RouterModel, router.id)
        if model is None or model.team_id != router.team_id:
            raise RouterNotFound(str(router.id))
        model.name = router.name
        model.candidates = _candidates_json(router)
        model.default_model = router.default_model
        model.strategy = router.strategy
        model.strategy_config = await self._map_config(
            self._preserve_masked_tokens(router.strategy_config, model.strategy_config),
            self._encrypt_section,
        )
        model.shadow_strategy = router.shadow_strategy
        model.enabled = router.enabled
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise RouterNameExists(router.name) from exc
        await self._session.refresh(model)
        return await self._to_entity(model)

    async def delete(self, team_id: UUID, router_id: UUID) -> bool:
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        result: Any = await self._session.execute(
            delete(RouterModel).where(RouterModel.id == router_id, RouterModel.team_id == team_id)
        )
        await self._session.commit()
        return bool(result.rowcount)


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
