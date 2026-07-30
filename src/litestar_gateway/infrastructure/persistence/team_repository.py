"""SQLAlchemy adapter implementing the `TeamRepository` port."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.callable_alias import CallableKind
from litestar_gateway.domain.entities import Team
from litestar_gateway.domain.exceptions import TeamNotEmpty
from litestar_gateway.domain.pagination import DEFAULT_PAGE_SIZE
from litestar_gateway.infrastructure.persistence.callable_alias_slots import (
    lock_resource_lifecycle,
    tombstone_resource,
)
from litestar_gateway.infrastructure.persistence.orm import (
    BudgetAlertStateModel,
    GuardrailRuleModel,
    InviteModel,
    McpServerGrantModel,
    McpServerModel,
    McpServerProposalModel,
    McpServerSuppressionModel,
    ModelGrantRecord,
    PendingBudgetAlertModel,
    PendingUsageEventModel,
    RouterGrantModel,
    RouterModel,
    RoutingDecisionModel,
    ServicePrincipalModel,
    TeamBudgetModel,
    TeamMembershipModel,
    TeamModel,
    UsageEventModel,
)


def _now() -> datetime:
    return datetime.now(UTC)


class SQLAlchemyTeamRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, team: Team) -> Team:
        # Stage only (flush, no commit); the service owns the transaction boundary.
        model = TeamModel(
            id=team.id,
            organization_id=team.organization_id,
            name=team.name,
            description=team.description,
            tags=list(team.tags),
            rate_limit_rpm=team.rate_limit_rpm,
        )
        self._session.add(model)
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def get(self, team_id: UUID) -> Team | None:
        # A soft-deleted (tombstoned) team reads as absent everywhere ordinary
        # operations look it up — `get_any` is the deliberate bypass.
        model = await self._session.get(TeamModel, team_id)
        return model.to_entity() if model and model.deleted_at is None else None

    async def get_any(self, team_id: UUID) -> Team | None:
        model = await self._session.get(TeamModel, team_id)
        return model.to_entity() if model else None

    async def has_billed_history(self, team_id: UUID) -> bool:
        # Either table having a row is "billed history": a settled ledger entry,
        # or a dead-lettered one still awaiting the reconciler — both represent
        # real spend that must not be silently lost by an ordinary delete.
        settled = await self._session.scalar(
            select(UsageEventModel.id).where(UsageEventModel.team_id == team_id).limit(1)
        )
        if settled is not None:
            return True
        pending = await self._session.scalar(
            select(PendingUsageEventModel.id)
            .where(PendingUsageEventModel.team_id == team_id)
            .limit(1)
        )
        return pending is not None

    async def soft_delete(self, team_id: UUID) -> Team | None:
        # Stage only (flush); the service owns the commit (unit of work).
        model = await self._session.get(TeamModel, team_id)
        if model is None:
            return None
        model.deleted_at = _now()
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def list_by_ids(self, team_ids: Sequence[UUID]) -> list[Team]:
        if not team_ids:
            return []
        models = await self._session.scalars(
            select(TeamModel).where(TeamModel.id.in_(team_ids), TeamModel.deleted_at.is_(None))
        )
        return [model.to_entity() for model in models]

    async def lock_for_lifecycle(self, team_id: UUID) -> Team | None:
        # A no-op write is a cross-database lifecycle mutex: PostgreSQL takes a
        # row-level NO KEY UPDATE lock and SQLite takes its writer lock. Raw SQL
        # avoids SQLAlchemy's automatic `updated_at` value on ORM updates.
        # A soft-deleted team is excluded, same as `get`: it reads as gone to
        # every ordinary lifecycle operation (delete-team, invite creation).
        statement = text(
            "UPDATE team SET name = name WHERE id = :team_id AND deleted_at IS NULL"
        ).bindparams(bindparam("team_id", type_=TeamModel.id.type))
        result: Any = await self._session.execute(statement, {"team_id": team_id})
        if result.rowcount != 1:
            return None
        model = await self._session.get(TeamModel, team_id)
        return model.to_entity() if model else None

    async def list_by_organization(
        self, organization_id: UUID, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0
    ) -> list[Team]:
        models = await self._session.scalars(
            select(TeamModel)
            .where(TeamModel.organization_id == organization_id, TeamModel.deleted_at.is_(None))
            .order_by(TeamModel.created_at, TeamModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def list(self, *, limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> list[Team]:
        models = await self._session.scalars(
            select(TeamModel)
            .where(TeamModel.deleted_at.is_(None))
            .order_by(TeamModel.created_at, TeamModel.id)
            .limit(limit)
            .offset(offset)
        )
        return [m.to_entity() for m in models]

    async def update(
        self,
        team_id: UUID,
        name: str,
        description: str | None,
        tags: Sequence[str],
        rate_limit_rpm: int | None,
    ) -> Team | None:
        # Stage only (flush); the service owns the commit (unit of work).
        model = await self._session.get(TeamModel, team_id)
        if model is None or model.deleted_at is not None:
            return None
        model.name = name
        model.description = description
        model.tags = list(tags)
        model.rate_limit_rpm = rate_limit_rpm
        await self._session.flush()
        await self._session.refresh(model)
        return model.to_entity()

    async def delete(self, team_id: UUID) -> None:
        # Remove the intrinsic children first (all FK team.id with RESTRICT, so
        # the team row can't drop while they exist), then the team. Models and
        # API keys are intentionally NOT touched — the caller refuses the delete
        # when any remain. Staged only; the service commits.
        #
        # The list below is every table that carries this team's id, whether or
        # not it has an FK (ISSUE-030):
        #   - budget-alert dedup ledger and outbox: FK team.id, so a team that
        #     ever crossed a threshold could not be deleted at all — the
        #     IntegrityError surfaced as a 409 TeamNotEmpty, a false denial;
        #   - grants the team RECEIVED (`model_grant`/`router_grant` rows owned
        #     by another team but pointing here): same FK, same false denial;
        #   - `pending_usage_event` and `routing_decision`: no FK, so they never
        #     blocked anything and were simply left behind. Routing decisions
        #     retain `user_text`/`system_prompt`, so leaving them turns an
        #     "irreversible purge" into a partial one.
        #   - `guardrail_rule` (ISSUE-040): same FK, same false denial, and it
        #     hit the ordinary delete too. Model- and router-scoped rules
        #     cascade with their model or router, so only the team-wide row —
        #     the common configuration — reached here, and it carries an
        #     envelope-encrypted signing secret that has to go with the purge.
        # What deliberately survives: the audit trail (including this purge's
        # own entry) and platform-level rows that are not team data.
        try:
            router_ids = list(
                await self._session.scalars(
                    select(RouterModel.id)
                    .where(RouterModel.team_id == team_id)
                    .order_by(RouterModel.id)
                )
            )
            for router_id in router_ids:
                router = await lock_resource_lifecycle(
                    self._session, CallableKind.ROUTER, router_id
                )
                if not isinstance(router, RouterModel) or router.team_id != team_id:
                    continue
                if await self._session.scalar(
                    select(RouterGrantModel.id)
                    .where(RouterGrantModel.router_id == router_id)
                    .limit(1)
                ):
                    raise TeamNotEmpty("team owns a shared router; revoke every router grant first")
                await tombstone_resource(self._session, CallableKind.ROUTER, router_id)
            for child in (
                InviteModel,
                UsageEventModel,
                PendingUsageEventModel,
                RoutingDecisionModel,
                PendingBudgetAlertModel,
                BudgetAlertStateModel,
                GuardrailRuleModel,
                # MCP: the team's own servers, the grants it received, and its
                # detaches of servers it does not own. Registered here in the
                # slice that creates the tables, not after a review finds them
                # missing — which is how ISSUE-040 was found for guardrail_rule.
                McpServerSuppressionModel,
                McpServerGrantModel,
                # Before `mcp_server`: a proposal references the server its
                # approval created, and deleting the servers first would leave the
                # FK to resolve on the way out.
                McpServerProposalModel,
                McpServerModel,
                ModelGrantRecord,
                RouterGrantModel,
                RouterModel,
                ServicePrincipalModel,
                TeamMembershipModel,
                TeamBudgetModel,
            ):
                await self._session.execute(delete(child).where(child.team_id == team_id))
            await self._session.execute(delete(TeamModel).where(TeamModel.id == team_id))
            await self._session.flush()
        except IntegrityError as exc:
            # A concurrent non-intrinsic child (model/API key) may appear after
            # the service recheck. Preserve the lifecycle contract as a 409,
            # never leak a database 500; the service UoW rolls everything back.
            raise TeamNotEmpty(str(team_id)) from exc
