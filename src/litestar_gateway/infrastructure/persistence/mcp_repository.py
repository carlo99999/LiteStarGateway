"""SQLAlchemy adapter for MCP tool servers (Plan 20 S1).

The one method worth reading carefully is `visible_to`: it is the single place
that answers "which servers can this team see", and it exists precisely so that
answer is never spelled `server.team_id == team_id` anywhere else.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.callable_alias import CallableOrigin
from litestar_gateway.domain.exceptions import (
    CredentialMisconfigured,
    InvalidMcpServer,
    SaltKeyMissing,
)
from litestar_gateway.domain.mcp import (
    ApiKeyToolPolicy,
    McpServer,
    McpServerGrant,
    McpTool,
    ToolEffect,
)
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.orm import (
    ApiKeyToolPolicyModel,
    McpServerGrantModel,
    McpServerModel,
    McpServerSuppressionModel,
    McpToolModel,
)

_AUTH_FIELD = "auth"


class SQLAlchemyMcpServerRepository:
    def __init__(self, session: AsyncSession, keyring: Keyring | None = None) -> None:
        self._session = session
        self._keyring = keyring

    def _require_keyring(self) -> Keyring:
        if self._keyring is None:
            raise SaltKeyMissing("SALT_KEY is not configured")
        return self._keyring

    async def get(self, server_id: UUID) -> McpServer | None:
        row = await self._session.get(McpServerModel, server_id)
        return row.to_entity() if row else None

    async def visible_to(self, team_id: UUID) -> list[McpServer]:
        """Own + extended + global, minus this team's detaches.

        One query, one place. A caller that filtered by `team_id` itself would
        drop globals and extended servers on the floor — the spelling behind the
        guardrail-scope gap and Round 12's ISSUE-020.
        """
        granted = select(McpServerGrantModel.server_id).where(
            McpServerGrantModel.team_id == team_id
        )
        suppressed = set(
            await self._session.scalars(
                select(McpServerSuppressionModel.server_id).where(
                    McpServerSuppressionModel.team_id == team_id
                )
            )
        )
        rows = await self._session.scalars(
            select(McpServerModel)
            .where(
                or_(
                    McpServerModel.team_id == team_id,
                    McpServerModel.team_id.is_(None),
                    McpServerModel.id.in_(granted),
                )
            )
            .order_by(McpServerModel.name)
        )
        visible: list[McpServer] = []
        for row in rows:
            if row.id in suppressed:
                continue
            if row.team_id == team_id:
                origin = CallableOrigin.OWN
            elif row.team_id is None:
                origin = CallableOrigin.GLOBAL
            else:
                origin = CallableOrigin.EXTENDED
            visible.append(dataclasses.replace(row.to_entity(), origin=origin))
        return visible

    async def add(self, server: McpServer, *, auth: str | None = None) -> McpServer:
        row = McpServerModel(
            id=server.id,
            team_id=server.team_id,
            name=server.name,
            url=server.url,
            enabled=server.enabled,
            tool_allowlist=list(server.tool_allowlist),
        )
        await self._apply_auth(row, auth)
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            # Check-then-write is not atomic: two concurrent creates with the
            # same name both pass the service's check and the loser hits the
            # unique constraint. Surfacing that as a 500 is ISSUE-049's mistake.
            await self._session.rollback()
            raise InvalidMcpServer(f"an MCP server named '{server.name}' already exists") from exc
        await self._session.refresh(row)
        return row.to_entity()

    async def update(self, server: McpServer, *, auth: str | None = None) -> McpServer:
        row = await self._session.get(McpServerModel, server.id)
        if row is None:
            raise InvalidMcpServer(str(server.id))
        row.name = server.name
        row.url = server.url
        row.enabled = server.enabled
        row.tool_allowlist = list(server.tool_allowlist)
        await self._apply_auth(row, auth)
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def remove(self, server_id: UUID) -> bool:
        result: Any = await self._session.execute(
            delete(McpServerModel).where(McpServerModel.id == server_id)
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def suppress(self, server_id: UUID, team_id: UUID) -> None:
        existing = await self._session.scalar(
            select(McpServerSuppressionModel).where(
                McpServerSuppressionModel.server_id == server_id,
                McpServerSuppressionModel.team_id == team_id,
            )
        )
        if existing is not None:
            return
        self._session.add(
            McpServerSuppressionModel(id=uuid4(), server_id=server_id, team_id=team_id)
        )
        await self._session.commit()

    async def unsuppress(self, server_id: UUID, team_id: UUID) -> bool:
        result: Any = await self._session.execute(
            delete(McpServerSuppressionModel).where(
                McpServerSuppressionModel.server_id == server_id,
                McpServerSuppressionModel.team_id == team_id,
            )
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def grant(self, server_id: UUID, team_id: UUID) -> None:
        existing = await self._session.scalar(
            select(McpServerGrantModel).where(
                McpServerGrantModel.server_id == server_id,
                McpServerGrantModel.team_id == team_id,
            )
        )
        if existing is not None:
            return
        self._session.add(McpServerGrantModel(id=uuid4(), server_id=server_id, team_id=team_id))
        await self._session.commit()

    async def list_global(self) -> list[McpServer]:
        """Global servers only — the platform admin's own inventory.

        Deliberately *not* expressed as `visible_to(some_team)`: that answers a
        team's question and would fold in whatever that team owns.
        """
        rows = await self._session.scalars(
            select(McpServerModel)
            .where(McpServerModel.team_id.is_(None))
            .order_by(McpServerModel.name)
        )
        # No origin fix-up needed: a NULL `team_id` reads as global from the row
        # itself, which is the one origin that does not depend on who is asking.
        return [row.to_entity() for row in rows]

    async def others_named(self, name: str, exclude_id: UUID) -> list[McpServer]:
        """Every other server carrying this name, whoever owns it.

        A server is referenced by name (there is no alias), so sharing a name
        across origins would put two spellings of "github" in front of one team.
        The application layer uses this to refuse the extension or promotion that
        would create the ambiguity, rather than resolving it silently later.
        """
        rows = await self._session.scalars(
            select(McpServerModel).where(
                McpServerModel.name == name, McpServerModel.id != exclude_id
            )
        )
        return [row.to_entity() for row in rows]

    async def list_grants(self, server_id: UUID) -> list[McpServerGrant]:
        rows = await self._session.scalars(
            select(McpServerGrantModel).where(McpServerGrantModel.server_id == server_id)
        )
        return [row.to_entity() for row in rows]

    async def revoke_grant_by_id(self, grant_id: UUID) -> bool:
        """By grant id, for the platform's un-extend endpoint — the `model_grant`
        shape, where the console holds the grant row it is revoking."""
        result: Any = await self._session.execute(
            delete(McpServerGrantModel).where(McpServerGrantModel.id == grant_id)
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def revoke_grant(self, server_id: UUID, team_id: UUID) -> bool:
        result: Any = await self._session.execute(
            delete(McpServerGrantModel).where(
                McpServerGrantModel.server_id == server_id,
                McpServerGrantModel.team_id == team_id,
            )
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def make_global(self, server_id: UUID) -> McpServer | None:
        row = await self._session.get(McpServerModel, server_id)
        if row is None:
            return None
        # Promotion drops the grants: a global server resolves to every team by
        # itself, so leaving them would be rows nothing reads — and a later
        # demotion should not silently restore a stale grant list.
        await self._session.execute(
            delete(McpServerGrantModel).where(McpServerGrantModel.server_id == server_id)
        )
        row.team_id = None
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def auth_token(self, server_id: UUID) -> str | None:
        row = await self._session.get(McpServerModel, server_id)
        if row is None or row.encrypted_auth is None or row.auth_key_id is None:
            return None
        cipher = await self._require_keyring().credential_cipher_for(row.auth_key_id)
        if cipher is None:  # pragma: no cover - a missing key row is not expected
            raise CredentialMisconfigured(f"encryption key for MCP server {row.id} is missing")
        return cipher.decrypt(row.encrypted_auth)[_AUTH_FIELD]

    async def tools(self, server_id: UUID) -> list[McpTool]:
        rows = await self._session.scalars(
            select(McpToolModel)
            .where(McpToolModel.server_id == server_id)
            .order_by(McpToolModel.name)
        )
        return [row.to_entity() for row in rows]

    async def replace_tools(self, server_id: UUID, tools: list[McpTool]) -> list[McpTool]:
        """Refresh the inventory while keeping each tool's declared effect.

        The inventory is a cache of what the server advertises; the effect is
        operator state. Re-reading it from the server on every refresh would make
        it a value the server controls, which is exactly what "declared, never
        detected" forbids.
        """
        existing = {
            row.name: row
            for row in await self._session.scalars(
                select(McpToolModel).where(McpToolModel.server_id == server_id)
            )
        }
        seen: set[str] = set()
        now = datetime.now(UTC)
        for tool in tools:
            seen.add(tool.name)
            row = existing.get(tool.name)
            if row is None:
                self._session.add(
                    McpToolModel(
                        id=uuid4(),
                        server_id=server_id,
                        name=tool.name,
                        description=tool.description,
                        schema=dict(tool.schema),
                        effect=tool.effect.value,
                        discovered_at=now,
                    )
                )
            else:
                row.description = tool.description
                row.schema = dict(tool.schema)
                row.discovered_at = now
        for name, row in existing.items():
            if name not in seen:
                await self._session.delete(row)
        await self._session.commit()
        return await self.tools(server_id)

    async def set_effect(self, server_id: UUID, tool_name: str, effect: ToolEffect) -> bool:
        row = await self._session.scalar(
            select(McpToolModel).where(
                McpToolModel.server_id == server_id, McpToolModel.name == tool_name
            )
        )
        if row is None:
            return False
        row.effect = effect.value
        await self._session.commit()
        return True

    async def _apply_auth(self, row: McpServerModel, auth: str | None) -> None:
        if auth is None:
            return
        key_id, cipher = await self._require_keyring().active_credential_cipher()
        row.encrypted_auth = cipher.encrypt({_AUTH_FIELD: auth})
        row.auth_key_id = key_id


class SQLAlchemyApiKeyToolPolicyRepository:
    """Per-key tool policy, on the `api_key_budget` adapter's shape: one row per
    key, replaced on write, absent meaning unrestricted."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, api_key_id: UUID) -> ApiKeyToolPolicy | None:
        row = await self._session.scalar(
            select(ApiKeyToolPolicyModel).where(ApiKeyToolPolicyModel.api_key_id == api_key_id)
        )
        return row.to_entity() if row else None

    async def set(self, policy: ApiKeyToolPolicy) -> ApiKeyToolPolicy:
        """Create or replace. Upsert rather than insert: the endpoint is a `PUT`,
        so a second call must not trip the unique constraint on `api_key_id`."""
        row = await self._session.scalar(
            select(ApiKeyToolPolicyModel).where(
                ApiKeyToolPolicyModel.api_key_id == policy.api_key_id
            )
        )
        if row is None:
            row = ApiKeyToolPolicyModel(
                id=uuid4(),
                api_key_id=policy.api_key_id,
                team_id=policy.team_id,
                allowed_tools=list(policy.allowed_tools),
                destructive_enabled=policy.destructive_enabled,
            )
            self._session.add(row)
        else:
            row.allowed_tools = list(policy.allowed_tools)
            row.destructive_enabled = policy.destructive_enabled
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def remove(self, api_key_id: UUID) -> bool:
        result: Any = await self._session.execute(
            delete(ApiKeyToolPolicyModel).where(ApiKeyToolPolicyModel.api_key_id == api_key_id)
        )
        await self._session.commit()
        return bool(result.rowcount)
