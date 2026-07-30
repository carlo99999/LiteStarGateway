"""Plan 20 S5 — the three properties a proposal flow has to have.

Each of these is a defect this codebase has already paid for, transplanted onto a
new surface before a review finds it here:

- **two concurrent approvals create one server.** The invite TOCTOU, which is why
  the claim is a conditional `UPDATE` and not a status a caller read and trusted.
  The test defeats the advisory read deliberately, because a test that lets the
  implementation re-read a committed row would pass against a read-then-write.
- **the allowlist is re-checked at approval.** ISSUE-034's lesson applied to a time
  gap: state validated when it was written must be re-validated when it becomes
  effective.
- **filing makes no outbound request.** Asserted with a discovery double that fails
  the test if it is called at all, rather than by counting requests that were
  supposed to be zero.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.application.mcp_proposal_service import McpProposalService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.egress_policy import EgressAllowlist, parse_allowlist
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import (
    InvalidMcpServer,
    McpDiscoveryFailed,
    McpProposalAlreadyDecided,
    McpProposalNotFound,
    PermissionDenied,
)
from litestar_gateway.domain.mcp import McpServer, McpTool, ProposalStatus, ToolEffect
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.mcp_repository import (
    SQLAlchemyMcpServerProposalRepository,
    SQLAlchemyMcpServerRepository,
)
from litestar_gateway.infrastructure.persistence.orm import McpServerProposalModel
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)

TEAM = uuid4()
OTHER_TEAM = uuid4()
ALLOWED = "https://tools.internal:8443/mcp"
PRINCIPAL = Principal(user=None, api_key=None)


class FakeTeams:
    """Grants or refuses the permission the service asks for, and records it."""

    def __init__(self, *, allow: bool = True) -> None:
        self._allow = allow
        self.asked: list[Permission] = []

    async def ensure_principal_team_permission(self, principal, team_id: UUID, permission):
        self.asked.append(permission)
        if not self._allow:
            raise PermissionDenied(str(permission))
        return None


class ForbiddenDiscovery:
    """A resolver double that fails the test if anything asks it to connect.

    The point of §2.4's deferral is that the *lowest* privilege in the system must
    not hold a primitive for making the gateway open a connection. A double that
    merely counted calls would let a regression pass with a plausible-looking
    `assert calls == 0` somewhere far from the code that made them; this one turns
    the request itself into the failure.
    """

    async def list_tools(self, server: McpServer, *, auth: str | None = None) -> list[McpTool]:
        raise AssertionError(f"filing a proposal reached the network: tools/list on {server.url!r}")


class RecordingDiscovery:
    """Answers one tool, and remembers the token it was handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def list_tools(self, server: McpServer, *, auth: str | None = None) -> list[McpTool]:
        self.calls.append((server.url, auth))
        return [
            McpTool(
                id=uuid4(),
                server_id=server.id,
                name="search",
                description="find things",
                schema={"type": "object"},
                effect=ToolEffect.READ,
            )
        ]


class LostRace:
    """The real repository as it looks to the approver that loses.

    `get` keeps reporting the proposal as pending — the read a losing caller made
    before the winner committed — while `decide` refuses, as the conditional
    `UPDATE` does once somebody else has claimed the row. That combination is the
    race, and it is not otherwise reachable deterministically from one process.
    """

    def __init__(self, inner: SQLAlchemyMcpServerProposalRepository) -> None:
        self._inner = inner
        self.claims = 0

    async def add(self, proposal, *, auth: str | None = None):
        return await self._inner.add(proposal, auth=auth)

    async def get(self, proposal_id: UUID):
        return await self._inner.get(proposal_id)

    async def list_for_team(self, team_id: UUID, *, status=None):
        return await self._inner.list_for_team(team_id, status=status)

    async def auth_token(self, proposal_id: UUID) -> str | None:
        return await self._inner.auth_token(proposal_id)

    async def decide(self, proposal_id: UUID, **kwargs) -> bool:
        """Refuse, and roll the caller's staged work back — what the real one does
        when its `WHERE status = 'pending'` matches nothing."""
        self.claims += 1
        await self._inner._session.rollback()  # noqa: SLF001 - mirrors the real refusal
        return False


class FailingDiscovery:
    def __init__(self) -> None:
        self.calls = 0

    async def list_tools(self, server: McpServer, *, auth: str | None = None) -> list[McpTool]:
        self.calls += 1
        raise McpDiscoveryFailed("the tool server is down")


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tools.internal` does not resolve on a laptop, and the allowlist resolves
    before matching. The pattern in `tests/egress/` is to patch the resolver so the
    test exercises the policy rather than the network."""
    import litestar_gateway.application.egress as egress_module

    async def resolve(host: str) -> list[str]:
        return ["10.9.0.7"]

    monkeypatch.setattr(egress_module, "_resolve_host_addresses", resolve)


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A maker rather than one session: the concurrency test needs two, on the same
    file, with independent identity maps."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'proposals.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _keyring(session: AsyncSession) -> Keyring:
    return Keyring(SQLAlchemySecretKeyRepository(session), "salt-key-material", "jwt-secret")


def _service(
    session: AsyncSession,
    *,
    teams: FakeTeams | None = None,
    allowlist: EgressAllowlist | None = None,
    discovery=None,
) -> McpProposalService:
    keyring = _keyring(session)
    return McpProposalService(
        SQLAlchemyMcpServerProposalRepository(session, keyring),
        SQLAlchemyMcpServerRepository(session, keyring),
        teams or FakeTeams(),
        allowlist=allowlist if allowlist is not None else parse_allowlist(("tools.internal:8443",)),
        discovery=discovery if discovery is not None else ForbiddenDiscovery(),
    )


def _servers(session: AsyncSession) -> SQLAlchemyMcpServerRepository:
    return SQLAlchemyMcpServerRepository(session, _keyring(session))


# ── filing ───────────────────────────────────────────────────────────────────


async def test_filing_a_proposal_makes_no_outbound_request(sessions) -> None:
    """The whole reason discovery is deferred to approval.

    The service is built with `ForbiddenDiscovery`, so a future refactor that
    "helpfully" validates a proposal by asking the server what it offers fails
    here rather than shipping a member-reachable egress primitive.
    """
    async with sessions() as session:
        service = _service(session, discovery=ForbiddenDiscovery())

        proposal = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)

        assert proposal.status is ProposalStatus.PENDING
        assert proposal.name == "github"
        # And nothing was registered: a pending proposal is not a server.
        assert await _servers(session).visible_to(TEAM) == []


async def test_filing_needs_only_propose_and_validates_the_url_offline(sessions) -> None:
    async with sessions() as session:
        teams = FakeTeams()
        service = _service(session, teams=teams)

        await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)

        # `tools:propose`, the one permission every team role holds — not
        # `tools:manage`, which would defeat the point of the whole flow.
        assert teams.asked == [Permission.TOOLS_PROPOSE]


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("http://tools.internal:8443/mcp", "https"),
        ("https://user:pw@tools.internal:8443/mcp", "userinfo"),  # pragma: allowlist secret
        ("https://attacker.example/mcp", "MCP_ALLOWED_HOSTS|not permitted"),
        ("https://tools.internal:9999/mcp", "MCP_ALLOWED_HOSTS|not permitted"),
    ],
)
async def test_a_proposal_cannot_smuggle_a_url_the_server_surface_refuses(
    sessions, url: str, match: str
) -> None:
    """The proposal path applies the identical veto, because it is literally the
    same function — a second copy is how one of them stops refusing something."""
    async with sessions() as session:
        with pytest.raises(InvalidMcpServer, match=match):
            await _service(session).file_proposal(PRINCIPAL, TEAM, name="evil", url=url)


async def test_the_token_is_stored_encrypted_and_never_surfaces_on_the_proposal(
    sessions,
) -> None:
    """The approver sees the name, the url and the requested tools — never the
    secret they are approving."""
    async with sessions() as session:
        service = _service(session)

        proposal = await service.file_proposal(
            PRINCIPAL, TEAM, name="github", url=ALLOWED, auth="pr0posed-token"
        )

        assert proposal.has_auth is True
        assert "pr0posed-token" not in repr(proposal)


# ── approval ─────────────────────────────────────────────────────────────────


async def test_approval_registers_the_server_and_runs_the_first_discovery(sessions) -> None:
    async with sessions() as session:
        discovery = RecordingDiscovery()
        service = _service(session, discovery=discovery)
        proposal = await service.file_proposal(
            PRINCIPAL, TEAM, name="github", url=ALLOWED, auth="pr0posed-token"
        )

        server = await service.approve_proposal(PRINCIPAL, TEAM, proposal.id)

        assert server.name == "github"
        assert server.url == ALLOWED
        # The token moved to the server it was filed for, and the discovery it
        # authorizes was made with it — an approval that dropped the token would
        # register a server that cannot authenticate.
        assert server.has_auth is True
        assert discovery.calls == [(ALLOWED, "pr0posed-token")]
        assert [tool.name for tool in await _servers(session).tools(server.id)] == ["search"]
        # The inventory was stamped, so the console reads "discovery ran".
        stored = await _servers(session).get(server.id)
        assert stored is not None and stored.last_discovered_at is not None
        decided = await service.list_proposals(PRINCIPAL, TEAM)
        assert decided[0].status is ProposalStatus.APPROVED
        assert decided[0].server_id == server.id


async def test_a_proposal_whose_host_left_the_allowlist_is_refused_at_approval(
    sessions,
) -> None:
    """ISSUE-034's lesson against a time gap rather than against DNS.

    The proposal was legal when filed. The allowlist that decides is the one in
    force *now*, so the second service below — the same deployment after an
    operator removed the host — must refuse it.
    """
    async with sessions() as session:
        filed = await _service(session).file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)

        # `MCP_ALLOWED_HOSTS` emptied: the fail-closed default the feature ships
        # with, reached by an operator revoking the entry. Given a *working*
        # discovery double on purpose — with `ForbiddenDiscovery` an approval that
        # skipped the re-check would fail on the network assertion instead, and the
        # diagnostic would name the wrong defect.
        narrowed = _service(
            session, allowlist=EgressAllowlist(entries=()), discovery=RecordingDiscovery()
        )
        with pytest.raises(InvalidMcpServer, match="MCP_ALLOWED_HOSTS|not permitted"):
            await narrowed.approve_proposal(PRINCIPAL, TEAM, filed.id)

        # Still pending, and nothing registered: the refusal is of the approval,
        # not of the proposal, so re-adding the host makes it approvable again.
        assert await _servers(session).visible_to(TEAM) == []
        still = await narrowed.list_proposals(PRINCIPAL, TEAM)
        assert still[0].status is ProposalStatus.PENDING


async def test_a_stale_view_cannot_claim_a_decision_somebody_else_already_made(
    sessions,
) -> None:
    """The invite TOCTOU as a repository property — the one test in this module
    that a read-then-write `decide` cannot pass.

    Every other test here would survive that implementation, because a second
    caller normally re-reads the row and sees the truth. The race is the case where
    it does *not*: it read `pending` before the winner committed, and acts on that.
    SQLAlchemy's identity map is weak, so simulating it needs a live reference to
    the loaded row — without one the losing session quietly fetches fresh data and
    the test proves nothing. `stale` below is that reference, and it is load-bearing.

    With the conditional `UPDATE` the claim matches no row and returns False. With
    a read-then-write it returns True off the stale row, and the caller goes on to
    register a second server for a proposal that was approved once.
    """
    async with sessions() as writer_session, sessions() as loser_session:
        proposal = await _service(writer_session).file_proposal(
            PRINCIPAL, TEAM, name="github", url=ALLOWED
        )
        writer = SQLAlchemyMcpServerProposalRepository(writer_session, _keyring(writer_session))
        loser = SQLAlchemyMcpServerProposalRepository(loser_session, _keyring(loser_session))

        stale = await loser_session.get(McpServerProposalModel, proposal.id)
        assert stale is not None and stale.status == ProposalStatus.PENDING.value

        decided_at = proposal.created_at
        assert decided_at is not None
        assert (
            await writer.decide(
                proposal.id,
                status=ProposalStatus.APPROVED,
                decided_by=None,
                decided_at=decided_at,
                server_id=uuid4(),
            )
            is True
        )
        await writer_session.commit()

        # The loser's view is genuinely behind: this is the read a read-then-write
        # implementation would trust.
        assert stale.status == ProposalStatus.PENDING.value
        assert (await loser.get(proposal.id)) is not None
        assert (await loser.get(proposal.id)).status is ProposalStatus.PENDING  # type: ignore[union-attr]

        # And the claim refuses anyway, which is the entire point.
        assert (
            await loser.decide(
                proposal.id,
                status=ProposalStatus.APPROVED,
                decided_by=None,
                decided_at=decided_at,
                server_id=uuid4(),
            )
            is False
        )


async def test_the_approver_that_loses_the_race_registers_nothing(sessions) -> None:
    """The service half of the property: it obeys the claim, and leaves no trace.

    A service that ignored what `decide` returned — or that committed the server
    before claiming — would leave a live, usable tool server behind for an approval
    it did not make. Nobody would notice: the proposal reads `approved` with the
    winner's `server_id`, and the extra server just sits in the team's registry.
    """
    async with sessions() as session:
        losing = LostRace(SQLAlchemyMcpServerProposalRepository(session, _keyring(session)))
        service = McpProposalService(
            losing,
            _servers(session),
            FakeTeams(),
            allowlist=parse_allowlist(("tools.internal:8443",)),
            discovery=RecordingDiscovery(),
        )
        proposal = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)

        with pytest.raises(McpProposalAlreadyDecided):
            await service.approve_proposal(PRINCIPAL, TEAM, proposal.id)

        assert losing.claims == 1  # it did try to claim...
        async with sessions() as auditor:
            assert await _servers(auditor).visible_to(TEAM) == []  # ...and left nothing


async def test_an_unreachable_tool_server_does_not_undo_a_settled_approval(
    sessions,
) -> None:
    """ISSUE-045's shape, refused up front: discovery runs after the decision is
    committed, so a tool server that is down leaves a registered server with no
    inventory — not a 502 the approver retries against an approved proposal."""
    async with sessions() as session:
        discovery = FailingDiscovery()
        service = _service(session, discovery=discovery)
        proposal = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)

        server = await service.approve_proposal(PRINCIPAL, TEAM, proposal.id)

        assert discovery.calls == 1
        assert await _servers(session).tools(server.id) == []
        # NULL, which the console already renders as "discovery never ran" — the
        # state S4 added the column to make expressible.
        stored = await _servers(session).get(server.id)
        assert stored is not None and stored.last_discovered_at is None
        assert (await service.list_proposals(PRINCIPAL, TEAM))[0].status is ProposalStatus.APPROVED


async def test_approving_a_name_another_server_already_took_leaves_the_proposal_pending(
    sessions,
) -> None:
    """The claim and the insert are one transaction.

    A proposal can sit pending while somebody registers that name directly. The
    approval then fails on the unique constraint — and must roll the claim back
    with it, because an `approved` row pointing at a server that was never created
    is a state nothing can repair.
    """
    async with sessions() as session:
        service = _service(session, discovery=RecordingDiscovery())
        proposal = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)
        await _servers(session).add(
            McpServer(
                id=uuid4(),
                team_id=TEAM,
                name="github",
                url=ALLOWED,
                enabled=True,
                created_at=datetime.now(UTC),
            )
        )

        with pytest.raises(InvalidMcpServer, match="already exists"):
            await service.approve_proposal(PRINCIPAL, TEAM, proposal.id)

        async with sessions() as auditor:
            fresh = SQLAlchemyMcpServerProposalRepository(auditor, _keyring(auditor))
            reread = await fresh.get(proposal.id)
            assert reread is not None
            assert reread.status is ProposalStatus.PENDING
            assert reread.server_id is None
            assert len(await _servers(auditor).visible_to(TEAM)) == 1


# ── rejection ────────────────────────────────────────────────────────────────


async def test_a_rejection_carries_a_reason_and_requires_one(sessions) -> None:
    async with sessions() as session:
        service = _service(session)
        proposal = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)

        with pytest.raises(InvalidMcpServer, match="reason"):
            await service.reject_proposal(PRINCIPAL, TEAM, proposal.id, reason="   ")

        rejected = await service.reject_proposal(
            PRINCIPAL, TEAM, proposal.id, reason="use the global one"
        )

        assert rejected.status is ProposalStatus.REJECTED
        assert rejected.reason == "use the global one"
        assert rejected.server_id is None
        assert await _servers(session).visible_to(TEAM) == []


async def test_a_decided_proposal_cannot_be_decided_again(sessions) -> None:
    """Neither direction: rejecting something just approved must fail rather than
    overwrite a decision a server already depends on."""
    async with sessions() as session:
        service = _service(session, discovery=RecordingDiscovery())
        proposal = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)
        await service.approve_proposal(PRINCIPAL, TEAM, proposal.id)

        with pytest.raises(McpProposalAlreadyDecided):
            await service.reject_proposal(PRINCIPAL, TEAM, proposal.id, reason="changed my mind")
        with pytest.raises(McpProposalAlreadyDecided):
            await service.approve_proposal(PRINCIPAL, TEAM, proposal.id)

        assert len(await _servers(session).visible_to(TEAM)) == 1


# ── tenancy ──────────────────────────────────────────────────────────────────


async def test_another_teams_proposal_is_absent_rather_than_forbidden(sessions) -> None:
    """A 403 would confirm the id exists, which is what `McpServerNotFound`
    already refuses to leak for servers."""
    async with sessions() as session:
        service = _service(session)
        proposal = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)

        with pytest.raises(McpProposalNotFound):
            await service.approve_proposal(PRINCIPAL, OTHER_TEAM, proposal.id)
        with pytest.raises(McpProposalNotFound):
            await service.reject_proposal(PRINCIPAL, OTHER_TEAM, proposal.id, reason="no")
        with pytest.raises(McpProposalNotFound):
            await service.approve_proposal(PRINCIPAL, TEAM, uuid4())

        assert await service.list_proposals(PRINCIPAL, OTHER_TEAM) == []


async def test_pending_only_filters_the_queue(sessions) -> None:
    async with sessions() as session:
        service = _service(session)
        keep = await service.file_proposal(PRINCIPAL, TEAM, name="github", url=ALLOWED)
        drop = await service.file_proposal(PRINCIPAL, TEAM, name="jira", url=ALLOWED)
        await service.reject_proposal(PRINCIPAL, TEAM, drop.id, reason="not needed")

        pending = await service.list_proposals(PRINCIPAL, TEAM, pending_only=True)

        assert [proposal.id for proposal in pending] == [keep.id]
        assert len(await service.list_proposals(PRINCIPAL, TEAM)) == 2
