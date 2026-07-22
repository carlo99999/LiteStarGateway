"""Transactional mutations for callable alias slots."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.infrastructure.persistence.orm import (
    CallableAliasRecord,
    ModelRecord,
    RouterModel,
)


async def lock_resource_lifecycle(
    session: AsyncSession, kind: CallableKind, resource_id: UUID
) -> ModelRecord | RouterModel | None:
    """The single, ordered mutex for every alias-affecting lifecycle write."""
    resource = ModelRecord if kind is CallableKind.MODEL else RouterModel
    if session.get_bind().dialect.name == "sqlite":
        # SQLite ignores SELECT FOR UPDATE. A no-op write takes its database
        # writer mutex and is held until commit, matching the lifecycle
        # serialization contract used in production. Raw SQL avoids the ORM's
        # automatic updated_at hook.
        table = "model" if kind is CallableKind.MODEL else "router"
        statement = text(f"UPDATE {table} SET name = name WHERE id = :resource_id").bindparams(
            bindparam("resource_id", type_=resource.id.type)
        )
        result: Any = await session.execute(statement, {"resource_id": resource_id})
        if result.rowcount != 1:
            return None
        return await session.get(resource, resource_id, populate_existing=True)
    return await session.scalar(
        select(resource)
        .where(resource.id == resource_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _scope(team_id: UUID | None):
    return (
        CallableAliasRecord.team_id.is_(None)
        if team_id is None
        else CallableAliasRecord.team_id == team_id
    )


async def claim_direct(
    session: AsyncSession,
    kind: CallableKind,
    resource_id: UUID,
    team_id: UUID | None,
    alias: str,
) -> None:
    slot = await session.scalar(
        select(CallableAliasRecord)
        .where(
            _scope(team_id),
            CallableAliasRecord.alias == alias,
            CallableAliasRecord.unavailable.is_(True),
        )
        .with_for_update()
    )
    values = {
        "model_id": resource_id if kind is CallableKind.MODEL else None,
        "router_id": resource_id if kind is CallableKind.ROUTER else None,
        "model_grant_id": None,
        "router_grant_id": None,
        "unavailable": False,
    }
    if slot is None:
        session.add(CallableAliasRecord(id=uuid4(), team_id=team_id, alias=alias, **values))
        return
    for field, value in values.items():
        setattr(slot, field, value)


async def rename_direct(
    session: AsyncSession,
    kind: CallableKind,
    resource_id: UUID,
    team_id: UUID | None,
    old_alias: str,
    new_alias: str,
) -> None:
    if old_alias == new_alias:
        return
    binding = await session.scalar(
        select(CallableAliasRecord).where(
            CallableAliasRecord.model_id == resource_id
            if kind is CallableKind.MODEL
            else CallableAliasRecord.router_id == resource_id,
            CallableAliasRecord.unavailable.is_(False),
            CallableAliasRecord.model_grant_id.is_(None),
            CallableAliasRecord.router_grant_id.is_(None),
        )
    )
    if binding is not None:
        binding.model_id = None
        binding.router_id = None
        binding.unavailable = True
        await session.flush()
    await claim_direct(session, kind, resource_id, team_id, new_alias)


async def promote_direct(
    session: AsyncSession, kind: CallableKind, resource_id: UUID, alias: str
) -> None:
    target = (
        CallableAliasRecord.model_id == resource_id
        if kind is CallableKind.MODEL
        else CallableAliasRecord.router_id == resource_id
    )
    local = await session.scalar(
        select(CallableAliasRecord).where(
            target,
            CallableAliasRecord.team_id.is_not(None),
            CallableAliasRecord.model_grant_id.is_(None),
            CallableAliasRecord.router_grant_id.is_(None),
            CallableAliasRecord.unavailable.is_(False),
        )
    )
    if local is None:  # pragma: no cover - repository invariant
        return
    tombstone = await session.scalar(
        select(CallableAliasRecord)
        .where(
            CallableAliasRecord.team_id.is_(None),
            CallableAliasRecord.alias == alias,
            CallableAliasRecord.unavailable.is_(True),
        )
        .with_for_update()
    )
    if tombstone is None:
        local.team_id = None
        return
    await session.delete(local)
    await session.flush()
    tombstone.model_id = resource_id if kind is CallableKind.MODEL else None
    tombstone.router_id = resource_id if kind is CallableKind.ROUTER else None
    tombstone.unavailable = False


async def tombstone_resource(session: AsyncSession, kind: CallableKind, resource_id: UUID) -> None:
    target = (
        CallableAliasRecord.model_id == resource_id
        if kind is CallableKind.MODEL
        else CallableAliasRecord.router_id == resource_id
    )
    await session.execute(
        update(CallableAliasRecord)
        .where(target, CallableAliasRecord.unavailable.is_(False))
        .values(
            model_id=None,
            router_id=None,
            model_grant_id=None,
            router_grant_id=None,
            unavailable=True,
        )
    )


async def tombstone_resource_grants(
    session: AsyncSession, kind: CallableKind, resource_id: UUID, canonical_alias: str
) -> None:
    target = (
        CallableAliasRecord.model_id == resource_id
        if kind is CallableKind.MODEL
        else CallableAliasRecord.router_id == resource_id
    )
    grant = (
        CallableAliasRecord.model_grant_id.is_not(None)
        if kind is CallableKind.MODEL
        else CallableAliasRecord.router_grant_id.is_not(None)
    )
    await session.execute(
        update(CallableAliasRecord)
        .where(
            target,
            grant,
            CallableAliasRecord.alias != canonical_alias,
            CallableAliasRecord.unavailable.is_(False),
        )
        .values(
            model_id=None,
            router_id=None,
            model_grant_id=None,
            router_grant_id=None,
            unavailable=True,
        )
    )


async def tombstone_grant(session: AsyncSession, kind: CallableKind, grant_id: UUID) -> None:
    target = (
        CallableAliasRecord.model_grant_id == grant_id
        if kind is CallableKind.MODEL
        else CallableAliasRecord.router_grant_id == grant_id
    )
    await session.execute(
        update(CallableAliasRecord)
        .where(target, CallableAliasRecord.unavailable.is_(False))
        .values(
            model_id=None,
            router_id=None,
            model_grant_id=None,
            router_grant_id=None,
            unavailable=True,
        )
    )
