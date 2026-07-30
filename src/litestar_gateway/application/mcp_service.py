"""MCP tool servers: the use cases (Plan 20 S1).

Two behaviours carry the design's weight and are worth reading before changing
anything here.

**The allowlist is the platform's only veto.** A team admin registers servers
freely, but `MCP_ALLOWED_HOSTS` bounds where any of them may point, and it is
checked here on write *and* again on every call by the dispatch path. Write-time
only is precisely the defect Round 15 found in the openai_compatible provider
(ISSUE-034): a name that resolved into the allowlisted range at save time and
elsewhere at call time was called anyway, and clearing the allowlist did not stop
existing credentials.

**`remove` is one verb with two effects.** Deleting a server the team owns
removes the resource; "deleting" a global or extended one detaches it for that
team alone. Collapsing those would let a team admin revoke a capability from every
other tenant, which is Round 12's ISSUE-020 re-made.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from litestar_gateway.application.egress import resolve_allowlisted_addresses
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.callable_alias import CallableOrigin
from litestar_gateway.domain.egress_policy import EgressAllowlist
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import InvalidMcpServer, McpServerNotFound
from litestar_gateway.domain.mcp import (
    ApiKeyToolPolicy,
    McpServer,
    McpServerGrant,
    McpTool,
    ToolEffect,
)
from litestar_gateway.domain.ports.mcp import ApiKeyToolPolicyRepository, McpServerRepository

MAX_NAME_LENGTH = 64


class McpServerService:
    def __init__(
        self,
        servers: McpServerRepository,
        teams,
        allowlist: EgressAllowlist | None = None,
    ) -> None:
        self._servers = servers
        self._teams = teams
        # Empty refuses everything, which is the fail-closed default the feature
        # ships with: a deployment that upgrades gains no egress reach until an
        # operator opts in, so no team can register a server yet.
        self._allowlist = allowlist or EgressAllowlist(entries=())

    # ── reads ────────────────────────────────────────────────────────────────

    async def list_servers(self, principal: Principal, team_id: UUID) -> list[McpServer]:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_READ
        )
        return await self._servers.visible_to(team_id)

    async def get_server(self, principal: Principal, team_id: UUID, server_id: UUID) -> McpServer:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_READ
        )
        return await self._require_visible(team_id, server_id)

    async def list_tools(
        self, principal: Principal, team_id: UUID, server_id: UUID
    ) -> list[McpTool]:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_READ
        )
        await self._require_visible(team_id, server_id)
        return await self._servers.tools(server_id)

    # ── writes ───────────────────────────────────────────────────────────────

    async def create_server(
        self,
        principal: Principal,
        team_id: UUID,
        *,
        name: str,
        url: str,
        auth: str | None = None,
        tool_allowlist: tuple[str, ...] = (),
        enabled: bool = True,
    ) -> McpServer:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        self._validate_name(name)
        await self._validate_url(url)
        return await self._servers.add(
            McpServer(
                id=uuid4(),
                team_id=team_id,
                name=name.strip(),
                url=url,
                enabled=enabled,
                created_at=datetime.now(UTC),
                tool_allowlist=tool_allowlist,
            ),
            auth=auth,
        )

    async def update_server(
        self,
        principal: Principal,
        team_id: UUID,
        server_id: UUID,
        *,
        name: str | None = None,
        url: str | None = None,
        auth: str | None = None,
        tool_allowlist: tuple[str, ...] | None = None,
        enabled: bool | None = None,
    ) -> McpServer:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        current = await self._require_owned(team_id, server_id)
        if name is not None:
            self._validate_name(name)
        if url is not None:
            await self._validate_url(url)
        updated = dataclasses.replace(
            current,
            name=(name.strip() if name is not None else current.name),
            url=url if url is not None else current.url,
            enabled=enabled if enabled is not None else current.enabled,
            tool_allowlist=(
                tool_allowlist if tool_allowlist is not None else current.tool_allowlist
            ),
        )
        return await self._servers.update(updated, auth=auth)

    async def remove_server(self, principal: Principal, team_id: UUID, server_id: UUID) -> str:
        """Delete the team's own server, or detach one it does not own.

        Returns which happened, because the caller has to audit them differently
        and a 204 that means two things is a trap for whoever reads the log.
        """
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        server = await self._require_visible(team_id, server_id)
        if server.team_id == team_id:
            await self._servers.remove(server_id)
            return "deleted"
        # Global or extended: never touch the resource other teams are using.
        await self._servers.suppress(server_id, team_id)
        return "detached"

    async def reattach_server(
        self, principal: Principal, team_id: UUID, server_id: UUID
    ) -> McpServer:
        """Undo a detach. A team that removed a global server can take it back
        without a platform admin, which is what makes the detach a reversible
        choice rather than a one-way door."""
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        if not await self._servers.unsuppress(server_id, team_id):
            raise McpServerNotFound(str(server_id))
        return await self._require_visible(team_id, server_id)

    async def declare_effect(
        self,
        principal: Principal,
        team_id: UUID,
        server_id: UUID,
        tool_name: str,
        effect: ToolEffect,
    ) -> None:
        """Effects are operator state, so only the server's owner sets them: a
        team that merely *sees* a global server must not be able to relabel a
        destructive tool as a read for everyone else."""
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        await self._require_owned(team_id, server_id)
        if not await self._servers.set_effect(server_id, tool_name, effect):
            raise McpServerNotFound(f"{server_id}/{tool_name}")

    # ── platform admin ───────────────────────────────────────────────────────
    #
    # No principal and no permission check: these are reached only through
    # `/platform/...`, whose `provide_current_admin` dependency has already
    # refused anyone who is not a platform admin — the same division
    # `ModelService.make_global` and `PlatformModelController` use. A team-scoped
    # method above must never call one of these.

    async def list_global_servers(self) -> list[McpServer]:
        return await self._servers.list_global()

    async def create_global_server(
        self,
        *,
        name: str,
        url: str,
        auth: str | None = None,
        tool_allowlist: tuple[str, ...] = (),
        enabled: bool = True,
    ) -> McpServer:
        """A server every team can see. The allowlist still applies: it bounds
        where the *gateway* may connect, so a platform admin is not exempt from
        it — only from the team permission check."""
        self._validate_name(name)
        await self._validate_url(url)
        await self._require_name_free(name, uuid4())
        return await self._servers.add(
            McpServer(
                id=uuid4(),
                team_id=None,
                name=name.strip(),
                url=url,
                enabled=enabled,
                created_at=datetime.now(UTC),
                tool_allowlist=tool_allowlist,
            ),
            auth=auth,
        )

    async def update_any_server(
        self,
        server_id: UUID,
        *,
        name: str | None = None,
        url: str | None = None,
        auth: str | None = None,
        tool_allowlist: tuple[str, ...] | None = None,
        enabled: bool | None = None,
    ) -> McpServer:
        current = await self._require_existing(server_id)
        if name is not None:
            self._validate_name(name)
            await self._require_name_free(name, server_id)
        if url is not None:
            await self._validate_url(url)
        updated = dataclasses.replace(
            current,
            name=(name.strip() if name is not None else current.name),
            url=url if url is not None else current.url,
            enabled=enabled if enabled is not None else current.enabled,
            tool_allowlist=(
                tool_allowlist if tool_allowlist is not None else current.tool_allowlist
            ),
        )
        return await self._servers.update(updated, auth=auth)

    async def delete_any_server(self, server_id: UUID) -> None:
        """The platform's delete, which really deletes — including a global one
        every team is using. The team-facing `remove_server` detaches instead,
        and keeping the two verbs in separate methods is what stops them
        collapsing into one that does the wrong thing for one of the callers."""
        await self._require_existing(server_id)
        await self._servers.remove(server_id)

    async def make_global(self, server_id: UUID) -> McpServer:
        server = await self._require_existing(server_id)
        if server.is_global:
            raise InvalidMcpServer("this server is already global")
        await self._require_name_free(server.name, server_id)
        promoted = await self._servers.make_global(server_id)
        if promoted is None:  # pragma: no cover - it existed one statement ago
            raise McpServerNotFound(str(server_id))
        return promoted

    async def extend(self, server_id: UUID, team_ids: tuple[UUID, ...]) -> list[McpServerGrant]:
        server = await self._require_existing(server_id)
        if server.is_global:
            # Nothing to extend: a global server already resolves to every team.
            raise InvalidMcpServer("a global server is already available to every team")
        for team_id in team_ids:
            if team_id == server.team_id:
                raise InvalidMcpServer("cannot extend a server to the team that owns it")
            await self._require_name_unseen(team_id, server.name)
        for team_id in team_ids:
            await self._servers.grant(server_id, team_id)
            # A team that detached this server before and is now granted it again
            # should see it: the detach was a choice about the previous grant.
            await self._servers.unsuppress(server_id, team_id)
        return await self._servers.list_grants(server_id)

    async def list_grants(self, server_id: UUID) -> list[McpServerGrant]:
        await self._require_existing(server_id)
        return await self._servers.list_grants(server_id)

    async def revoke_grant(self, grant_id: UUID) -> None:
        if not await self._servers.revoke_grant_by_id(grant_id):
            raise McpServerNotFound(str(grant_id))

    # ── internals ────────────────────────────────────────────────────────────

    async def _require_visible(self, team_id: UUID, server_id: UUID) -> McpServer:
        """Resolved through the repository's single visibility union, never by
        comparing `team_id` here — the spelling that hides globals (D5)."""
        for server in await self._servers.visible_to(team_id):
            if server.id == server_id:
                return server
        raise McpServerNotFound(str(server_id))

    async def _require_existing(self, server_id: UUID) -> McpServer:
        """Platform-side lookup: by id, regardless of owner. The team-side
        `_require_visible` exists so a team cannot reach past its own tenancy;
        a platform admin has no tenancy to stay inside."""
        server = await self._servers.get(server_id)
        if server is None:
            raise McpServerNotFound(str(server_id))
        return server

    async def _require_name_free(self, name: str, server_id: UUID) -> None:
        """A name about to become visible to every team must be unique globally.

        There is no alias for a server, so a global "github" alongside a team's
        own "github" would give that team two servers under one name. Refusing
        here is what keeps the S6 reference `{"server": "github"}` unambiguous
        without inventing renaming-on-extend.
        """
        clashing = await self._servers.others_named(name.strip(), server_id)
        if clashing:
            raise InvalidMcpServer(
                f"another server is already named {name.strip()!r}; a server visible "
                "to every team needs a name no team is already using"
            )

    async def _require_name_unseen(self, team_id: UUID, name: str) -> None:
        for server in await self._servers.visible_to(team_id):
            if server.name == name:
                raise InvalidMcpServer(
                    f"team {team_id} already sees a server named {name!r}; rename one "
                    "of them before extending"
                )

    async def _require_owned(self, team_id: UUID, server_id: UUID) -> McpServer:
        server = await self._require_visible(team_id, server_id)
        if server.origin is not CallableOrigin.OWN:
            raise InvalidMcpServer(
                "this server belongs to the platform or another team: it can be "
                "detached from this team, but not edited here"
            )
        return server

    def _validate_name(self, name: str) -> None:
        if not name.strip():
            raise InvalidMcpServer("name must not be empty")
        if len(name) > MAX_NAME_LENGTH:
            raise InvalidMcpServer(f"name must be at most {MAX_NAME_LENGTH} characters")

    async def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            # The payload carries tool arguments derived from user prompts, and a
            # verdict comes back that the model will act on. Cleartext is not a
            # configuration an operator makes by accident.
            raise InvalidMcpServer(f"url must be https, got {url!r}")
        if parsed.username is not None or parsed.password is not None:
            # ISSUE-048's lesson: the endpoint is kept in the clear for logs and
            # metric labels, so a password in the URL would be logged verbatim.
            raise InvalidMcpServer(
                "url must not carry userinfo (user:password@host); use the auth token instead"
            )
        try:
            host, port = parsed.hostname, parsed.port
        except ValueError as exc:
            raise InvalidMcpServer(f"url is not usable: {exc}") from exc
        if not host:
            raise InvalidMcpServer(f"url has no host, got {url!r}")
        try:
            await resolve_allowlisted_addresses(host, port, self._allowlist)
        except ValueError as exc:
            # Not `str(exc)`: the shared resolver names the *provider* variable
            # (`OPENAI_COMPATIBLE_ALLOWED_HOSTS`) in its message, and this surface
            # is bounded by `MCP_ALLOWED_HOSTS`. Passing it through would send an
            # operator to edit the wrong setting.
            raise InvalidMcpServer(
                f"host {host!r} (port {port}) is not permitted by MCP_ALLOWED_HOSTS"
            ) from exc
        except OSError as exc:
            # `getaddrinfo` raises `gaierror` (an OSError), not ValueError, so a
            # host that does not resolve would otherwise surface as a 500. An
            # unresolvable target is a misconfiguration the operator can fix, so
            # it gets the same 400 as a target outside the allowlist.
            raise InvalidMcpServer(f"url host {host!r} could not be resolved: {exc}") from exc


class ApiKeyToolPolicyService:
    """Which tools one API key may invoke (design §2.5).

    Both the read and the write live under `tools:read`/`tools:manage`, **not**
    `keys:issue`. That is Round 15's ISSUE-042 applied before the fact: per-key
    spend caps had the write under `keys:issue` and the read under `budget:read`,
    so a key issuer could `PUT` an object and then get a 403 reading it back.
    Choosing which tools a key may call is a tool decision that happens to be
    addressed by key, so it stays in the tools domain on both sides.
    """

    def __init__(self, policies: ApiKeyToolPolicyRepository, teams, keys) -> None:
        self._policies = policies
        self._teams = teams
        self._keys = keys

    async def get_policy(
        self, principal: Principal, team_id: UUID, key_id: UUID
    ) -> ApiKeyToolPolicy | None:
        """`None` means unrestricted, which is a real answer rather than a 404:
        every key without a row can call every non-destructive tool."""
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_READ
        )
        await self._require_key(team_id, key_id)
        return await self._policies.get(key_id)

    async def set_policy(
        self,
        principal: Principal,
        team_id: UUID,
        key_id: UUID,
        *,
        allowed_tools: tuple[str, ...],
        destructive_enabled: bool,
    ) -> ApiKeyToolPolicy:
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        await self._require_key(team_id, key_id)
        return await self._policies.set(
            ApiKeyToolPolicy(
                id=uuid4(),
                api_key_id=key_id,
                team_id=team_id,
                allowed_tools=allowed_tools,
                destructive_enabled=destructive_enabled,
            )
        )

    async def remove_policy(self, principal: Principal, team_id: UUID, key_id: UUID) -> bool:
        """Removing the policy makes the key permissive again, so it is a
        widening, not a cleanup — which is why the caller audits it."""
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        await self._require_key(team_id, key_id)
        return await self._policies.remove(key_id)

    async def _require_key(self, team_id: UUID, key_id: UUID) -> None:
        """Resolved through the team, so a key id from another tenant is a 404
        here rather than a policy written across teams — the same reason the
        per-key budget endpoints resolve the key before touching the cap."""
        await self._keys.get_active_for_team(team_id, key_id)
