"""MCP server proposals: the queue between a member and an admin (Plan 20 S5).

Authorization is not repeated here — `McpProposalService` demands
`tools:propose` or `tools:manage` on every call, so a future entry point inherits
the check rather than having to remember it.

Two things about this surface are worth knowing before changing it:

**Approval and rejection are `POST`s, not a `PATCH` of `status`.** A field a client
sets to an arbitrary value would make `pending → pending` and `approved →
rejected` expressible, and the state machine only has two transitions. Two verbs
means the gateway owns which ones exist.

**A lost race is a 409, not a 500 and not a silent success.** Two admins clicking
approve concurrently is an ordinary event in a console with a shared queue, and the
second one is told plainly that somebody else got there first.

A platform admin needs no separate surface: `ensure_principal_team_permission`
already lets one act inside any team, which is the "a team admin *or* a platform
admin decides" of design §2.4.
"""

from __future__ import annotations

from uuid import UUID

from litestar import Controller, Request, get, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_gateway.application.mcp_proposal_service import McpProposalService
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.mcp.schemas import (
    McpServerProposalResponse,
    McpServerResponse,
    ProposeMcpServerRequest,
    RejectMcpProposalRequest,
)
from litestar_gateway.infrastructure.web.principal import provide_principal


class McpProposalController(Controller):
    path = "/teams"
    tags = ["mcp"]
    dependencies = {"principal": Provide(provide_principal)}

    @get(
        "/{team_id:uuid}/mcp-server-proposals",
        summary="The team's tool-server proposals",
        description=(
            "Under `tools:propose`, which every team role holds: the member who "
            "filed a proposal has to be able to read the decision and, on a "
            "rejection, the reason. Bearer tokens are never returned."
        ),
    )
    async def list_proposals(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        mcp_proposal_service: NamedDependency[McpProposalService],
        pending: FromQuery[bool | None] = None,
    ) -> list[McpServerProposalResponse]:
        proposals = await mcp_proposal_service.list_proposals(
            principal, team_id, pending_only=bool(pending)
        )
        return [McpServerProposalResponse.from_entity(proposal) for proposal in proposals]

    @post(
        "/{team_id:uuid}/mcp-server-proposals",
        summary="Ask the team's admins to register an MCP server",
        description=(
            "Files a request under `tools:propose`. It changes no policy and makes "
            "**no outbound request**: the url is validated offline, and the gateway "
            "only contacts the server once an admin approves. Nothing on the call "
            "path reads a pending proposal."
        ),
    )
    async def file_proposal(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: ProposeMcpServerRequest,
        principal: NamedDependency[Principal],
        mcp_proposal_service: NamedDependency[McpProposalService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerProposalResponse:
        proposal = await mcp_proposal_service.file_proposal(
            principal,
            team_id,
            name=data.name,
            url=data.url,
            auth=data.auth,
            tool_allowlist=tuple(data.tool_allowlist or ()),
        )
        await record_audit(
            audit_log,
            request,
            principal.user,
            "mcp_server_proposal.file",
            target_type="mcp_server_proposal",
            target_id=proposal.id,
            # The endpoint, as on `mcp_server.create`: which host somebody asked
            # the gateway to reach is the fact an audit reader needs.
            detail=f"'{proposal.name}' → {proposal.url}",
        )
        return McpServerProposalResponse.from_entity(proposal)

    @post(
        "/{team_id:uuid}/mcp-server-proposals/{proposal_id:uuid}/approve",
        summary="Approve a proposal and register the server",
        description=(
            "Registers the server exactly as filed and runs the first tool "
            "discovery. `MCP_ALLOWED_HOSTS` is re-checked here: a proposal whose "
            "host has since left the allowlist is refused with a 400 and stays "
            "pending. Concurrent approvals produce one server — the loser gets 409."
        ),
        status_code=201,
    )
    async def approve_proposal(
        self,
        request: Request,
        team_id: FromPath[UUID],
        proposal_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        mcp_proposal_service: NamedDependency[McpProposalService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerResponse:
        server = await mcp_proposal_service.approve_proposal(principal, team_id, proposal_id)
        # Audited on both outcomes, and as its own action rather than a detail on a
        # shared one: an operator asking who authorized this egress should not have
        # to read details to exclude the refusals.
        await record_audit(
            audit_log,
            request,
            principal.user,
            "mcp_server_proposal.approve",
            target_type="mcp_server_proposal",
            target_id=proposal_id,
            detail=f"registered '{server.name}' → {server.url} as {server.id}",
        )
        return McpServerResponse.from_entity(server)

    @post(
        "/{team_id:uuid}/mcp-server-proposals/{proposal_id:uuid}/reject",
        summary="Refuse a proposal, with a reason",
        description=(
            "The reason is required and is what the member who filed it reads. "
            "There is no edit: an admin who wants different settings registers the "
            "server directly."
        ),
        status_code=200,
    )
    async def reject_proposal(
        self,
        request: Request,
        team_id: FromPath[UUID],
        proposal_id: FromPath[UUID],
        data: RejectMcpProposalRequest,
        principal: NamedDependency[Principal],
        mcp_proposal_service: NamedDependency[McpProposalService],
        audit_log: NamedDependency[AuditLog],
    ) -> McpServerProposalResponse:
        proposal = await mcp_proposal_service.reject_proposal(
            principal, team_id, proposal_id, reason=data.reason
        )
        await record_audit(
            audit_log,
            request,
            principal.user,
            "mcp_server_proposal.reject",
            target_type="mcp_server_proposal",
            target_id=proposal_id,
            detail=f"'{proposal.name}' refused: {proposal.reason}",
        )
        return McpServerProposalResponse.from_entity(proposal)
