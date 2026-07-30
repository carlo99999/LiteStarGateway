"""MCP server proposals: a member asks, an admin decides (Plan 20 S5, §2.4).

Requiring `tools:manage` for every registration puts the person who knows *which
tool the application needs* behind the person who holds the permission. A proposal
splits that: any member may file one, and an admin approves or rejects it. There
was no approval workflow in this codebase, so this mirrors the only two-party flow
that existed — **invites** — and inherits its one hard-won property.

Three rules carry the whole slice, and each is a defect this project has already
paid for somewhere else:

**Approval is a single conditional UPDATE, never a read-then-write.** Two admins
clicking approve at the same moment must produce one server. `decide` returns
whether *it* claimed the row; nothing here believes a status it read a moment
earlier, because by the time it acts that status may be a lie. This is the invite
TOCTOU fix, applied before the finding rather than after it.

**Approval re-validates; it never trusts the proposal.** `MCP_ALLOWED_HOSTS` is
checked again here, not only when the row was written. A host allowlisted on Monday
and removed on Tuesday must not become a live server on Wednesday because a pending
record still remembers it — ISSUE-034's lesson (state validated when written must
be re-validated when it becomes effective) applied to a time gap instead of to DNS.

**No member action causes gateway egress.** Filing validates the url's shape and
its allowlist membership, both offline. `tools/list` runs at *approval*, when a
privileged actor has decided the target is legitimate. Otherwise the lowest
privilege in the system would hold a primitive for making the gateway connect
somewhere — and the service is built with `discovery=None` on any path that must
not be able to, which is a stronger statement than "it happens not to call it".
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from litestar_gateway.application.mcp_service import validate_mcp_name, validate_mcp_url
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.egress_policy import EgressAllowlist
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import (
    InvalidMcpServer,
    McpDiscoveryFailed,
    McpProposalAlreadyDecided,
    McpProposalNotFound,
)
from litestar_gateway.domain.mcp import McpServer, McpServerProposal, ProposalStatus
from litestar_gateway.domain.ports.mcp import (
    McpDiscoveryPort,
    McpServerProposalRepository,
    McpServerRepository,
)

MAX_REASON_LENGTH = 500


class McpProposalService:
    def __init__(
        self,
        proposals: McpServerProposalRepository,
        servers: McpServerRepository,
        teams,
        allowlist: EgressAllowlist | None = None,
        discovery: McpDiscoveryPort | None = None,
    ) -> None:
        self._proposals = proposals
        self._servers = servers
        self._teams = teams
        # The same rule the server service applies, so the proposal path cannot be
        # a way around it: empty falls through to the SSRF deny-list, an entry
        # authorizes an internal target, and a configured list bounds everything.
        self._allowlist = allowlist or EgressAllowlist(entries=())
        self._discovery = discovery

    # ── the member's side ────────────────────────────────────────────────────

    async def file_proposal(
        self,
        principal: Principal,
        team_id: UUID,
        *,
        name: str,
        url: str,
        auth: str | None = None,
        tool_allowlist: tuple[str, ...] = (),
    ) -> McpServerProposal:
        """Under `tools:propose`, which every team role holds — including
        `member`, the role `authorization.py` says holds nothing on purpose.

        That principle is not weakened, because a proposal changes no policy and
        reaches no network: it validates the url offline and writes a row nothing
        on the call path reads. The permission is the *ask*, not the effect.
        """
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_PROPOSE
        )
        validate_mcp_name(name)
        # Offline: shape, scheme, userinfo, and allowlist membership. The one
        # thing it must not do is open a connection, which is why discovery lives
        # in `approve_proposal` and not here.
        await validate_mcp_url(url, self._allowlist)
        return await self._proposals.add(
            McpServerProposal(
                id=uuid4(),
                team_id=team_id,
                proposed_by=principal.user.id if principal.user else None,
                name=name.strip(),
                url=url,
                tool_allowlist=tool_allowlist,
            ),
            auth=auth,
        )

    async def list_proposals(
        self,
        principal: Principal,
        team_id: UUID,
        *,
        pending_only: bool = False,
    ) -> list[McpServerProposal]:
        """Readable under `tools:propose` rather than `tools:read`.

        The member who filed one has to be able to read the decision and, on a
        rejection, the reason — "it disappeared" is not an answer. Nothing here
        carries a secret: the queue shows names, urls and `has_auth`, so widening
        the read to every role exposes what a teammate already told the team they
        wanted, and nothing more.
        """
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_PROPOSE
        )
        status = ProposalStatus.PENDING if pending_only else None
        return await self._proposals.list_for_team(team_id, status=status)

    # ── the approver's side ──────────────────────────────────────────────────

    async def approve_proposal(
        self, principal: Principal, team_id: UUID, proposal_id: UUID
    ) -> McpServer:
        """Register the proposed server, exactly as filed.

        The order of the four steps below is the design, not a preference:

        1. **re-validate the allowlist** — before anything is written, so a
           proposal whose host has since left `MCP_ALLOWED_HOSTS` stays pending
           and an operator can either fix the config or reject it with a reason;
        2. **stage the server**, uncommitted. A name taken while the proposal sat
           pending fails here, before any decision is claimed, so the proposal
           stays pending rather than becoming an approval of nothing. It has to
           precede the claim for a second reason too: the claim records
           `server_id`, and a foreign key does not accept a row that does not
           exist yet — which is how the wired test found the first draft;
        3. **claim the row**, one conditional `UPDATE`. It commits the staged
           server with itself, or rolls it back if somebody else got there first —
           so a loser creates nothing;
        4. **discover**, last and non-fatally. The approval is settled by then, and
           turning a settled success into an error because a tool server was
           unreachable is ISSUE-045's shape.
        """
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        proposal = await self._require_pending(team_id, proposal_id)
        # (1) The check that makes a pending row a request rather than a promise.
        await validate_mcp_url(proposal.url, self._allowlist)
        # Read before anything is staged: `auth_token` is a plain SELECT, and doing
        # it later would put a decrypt between the insert and the claim.
        auth = await self._proposals.auth_token(proposal_id)

        server = McpServer(
            id=uuid4(),
            team_id=team_id,
            name=proposal.name,
            url=proposal.url,
            enabled=True,
            created_at=datetime.now(UTC),
            tool_allowlist=proposal.tool_allowlist,
            has_auth=auth is not None,
        )
        # (2) Written, not committed.
        await self._servers.stage_add(server, auth=auth)
        # (3) The gate. Everything above was advisory, and everything above is
        # discarded if this refuses.
        claimed = await self._proposals.decide(
            proposal_id,
            status=ProposalStatus.APPROVED,
            decided_by=principal.user.id if principal.user else None,
            decided_at=datetime.now(UTC),
            server_id=server.id,
        )
        if not claimed:
            raise McpProposalAlreadyDecided(str(proposal_id))
        await self._discover_quietly(server, auth)
        return server

    async def reject_proposal(
        self, principal: Principal, team_id: UUID, proposal_id: UUID, *, reason: str
    ) -> McpServerProposal:
        """A refusal carries why. Approvers cannot edit a proposal, so a rejection
        with a reason is the only channel back to the member who filed it."""
        await self._teams.ensure_principal_team_permission(
            principal, team_id, Permission.TOOLS_MANAGE
        )
        await self._require_pending(team_id, proposal_id)
        if not reason.strip():
            raise InvalidMcpServer("a rejection must carry a reason")
        if len(reason) > MAX_REASON_LENGTH:
            raise InvalidMcpServer(f"reason must be at most {MAX_REASON_LENGTH} characters")
        # The same conditional UPDATE as approval: rejecting a row somebody just
        # approved must fail rather than overwrite the decision.
        claimed = await self._proposals.decide(
            proposal_id,
            status=ProposalStatus.REJECTED,
            decided_by=principal.user.id if principal.user else None,
            decided_at=datetime.now(UTC),
            reason=reason.strip(),
        )
        if not claimed:
            raise McpProposalAlreadyDecided(str(proposal_id))
        decided = await self._proposals.get(proposal_id)
        if decided is None:  # pragma: no cover - it was updated one statement ago
            raise McpProposalNotFound(str(proposal_id))
        return decided

    # ── internals ───────────────────────────────────────────────────────────

    async def _discover_quietly(self, server: McpServer, auth: str | None) -> None:
        """Discovery deferred to approval, and allowed to fail.

        The server is registered and the proposal decided before this runs. A tool
        server that is down leaves `last_discovered_at` NULL — which the console
        already renders as "discovery never ran" — instead of turning a completed
        approval into a 502 the approver would retry against an already-approved
        proposal.
        """
        if self._discovery is None:
            return
        try:
            discovered = await self._discovery.list_tools(server, auth=auth)
        except McpDiscoveryFailed:
            return
        await self._servers.replace_tools(server.id, discovered)

    async def _require_pending(self, team_id: UUID, proposal_id: UUID) -> McpServerProposal:
        """Advisory, and only advisory.

        It exists so a caller gets 404 for a proposal that is not theirs and 409
        for one already decided, instead of both arriving as a bare failed claim.
        The claim is still what decides — this read may be stale by the time it
        returns, and the design only works if nothing depends on it not being.
        """
        proposal = await self._proposals.get(proposal_id)
        # Team mismatch reads as absent: a 403 would confirm another tenant's
        # proposal exists, the same reason `McpServerNotFound` covers both cases.
        if proposal is None or proposal.team_id != team_id:
            raise McpProposalNotFound(str(proposal_id))
        if not proposal.is_pending:
            raise McpProposalAlreadyDecided(
                f"proposal {proposal_id} was already {proposal.status.value}"
            )
        return proposal
