"""R7-L36: OFFSET pagination that orders only by `created_at` gives DB-arbitrary
order to rows with a tied timestamp, so paging by limit/offset can skip or
duplicate rows across pages. Appending the primary key `id` as a final
`order_by` term (the pattern already used by `SQLAlchemyUserRepository.list`)
makes the order deterministic even when timestamps collide.

Behavior alone can't distinguish "ordered, tiebreaker included" from "ordered,
tiebreaker absent but the engine's incidental tie order happens to be stable"
(SQLite resolves `created_at` ties by physical row order, which stays stable
across separate LIMIT/OFFSET queries in these tests regardless of the fix).
So these tests inspect the compiled SQL's ORDER BY clause directly, asserting
`id` is the final ordering column — the same thing a query-plan review would
check.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy import Select

from litestar_gateway.infrastructure.persistence.audit_repository import SQLAlchemyAuditLog
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.membership_repository import (
    SQLAlchemyTeamMembershipRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)
from litestar_gateway.infrastructure.persistence.organization_repository import (
    SQLAlchemyOrganizationRepository,
)
from litestar_gateway.infrastructure.persistence.repository import SQLAlchemyAPIKeyRepository
from litestar_gateway.infrastructure.persistence.router_repository import (
    SQLAlchemyRoutingDecisionLog,
)
from litestar_gateway.infrastructure.persistence.service_principal_repository import (
    SQLAlchemyServicePrincipalRepository,
)
from litestar_gateway.infrastructure.persistence.team_repository import SQLAlchemyTeamRepository
from litestar_gateway.infrastructure.persistence.usage_repository import SQLAlchemyUsageRepository


class _EmptyRows:
    def all(self) -> list[Any]:
        return []


class _CapturingSession:
    """Stands in for AsyncSession: records the statement a repo builds instead
    of executing it, so the query shape can be inspected directly."""

    def __init__(self) -> None:
        self.captured: Select[Any] | None = None

    async def scalars(self, stmt: Select[Any]) -> list[Any]:
        self.captured = stmt
        return []

    async def execute(self, stmt: Select[Any]) -> _EmptyRows:
        self.captured = stmt
        return _EmptyRows()


def _order_by_sql(stmt: Select[Any]) -> str:
    # No literal_binds: parameter values (UUIDs) don't have a generic-dialect
    # literal renderer, and only the ORDER BY column names matter here.
    compiled = str(stmt.compile())
    match = re.search(r"ORDER BY (.*?)(?:\n? LIMIT|\Z)", compiled, re.S)
    assert match, f"no ORDER BY clause found in compiled query: {compiled}"
    return match.group(1)


async def test_membership_list_by_team_order_by_includes_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyTeamMembershipRepository(session)  # type: ignore[arg-type]
    await repo.list_by_team(uuid4(), limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert order_by.rstrip().endswith("team_membership.id"), order_by


async def test_model_list_by_team_order_by_includes_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyModelRepository(session)  # type: ignore[arg-type]
    await repo.list_by_team(uuid4(), limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert re.search(r"\bid\b", order_by), order_by


async def test_service_principal_list_by_team_order_by_includes_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyServicePrincipalRepository(session)  # type: ignore[arg-type]
    await repo.list_by_team(uuid4(), limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert re.search(r"\bid\b", order_by), order_by


async def test_credential_list_order_by_includes_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyCredentialRepository(session)  # type: ignore[arg-type]
    await repo.list(limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert re.search(r"\bid\b", order_by), order_by


async def test_audit_list_recent_order_by_includes_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyAuditLog(session)  # type: ignore[arg-type]
    await repo.list_recent(limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert re.search(r"\bid\b", order_by), order_by


async def test_routing_decision_list_decisions_order_by_includes_id() -> None:
    session = _CapturingSession()
    log = SQLAlchemyRoutingDecisionLog(session)  # type: ignore[arg-type]
    await log.list_decisions(uuid4(), "router-a", limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert re.search(r"\bid\b", order_by), order_by


async def test_organization_list_order_by_includes_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyOrganizationRepository(session)  # type: ignore[arg-type]
    await repo.list(limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert order_by.rstrip().endswith("organization.id"), order_by


async def test_team_lists_order_by_include_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyTeamRepository(session)  # type: ignore[arg-type]

    await repo.list(limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert order_by.rstrip().endswith("team.id"), order_by

    await repo.list_by_organization(uuid4(), limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert order_by.rstrip().endswith("team.id"), order_by


async def test_api_key_lists_order_by_include_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyAPIKeyRepository(session)  # type: ignore[arg-type]

    await repo.list_by_team(uuid4(), limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert order_by.rstrip().endswith("api_key.id"), order_by

    await repo.list_by_creator(uuid4(), limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "created_at" in order_by
    assert order_by.rstrip().endswith("api_key.id"), order_by


async def test_usage_aggregate_order_by_includes_model_id() -> None:
    session = _CapturingSession()
    repo = SQLAlchemyUsageRepository(session)  # type: ignore[arg-type]
    await repo.aggregate(uuid4(), limit=1, offset=0)
    assert session.captured is not None
    order_by = _order_by_sql(session.captured)
    assert "model_name" in order_by
    assert order_by.rstrip().endswith("usage_event.model_id"), order_by
