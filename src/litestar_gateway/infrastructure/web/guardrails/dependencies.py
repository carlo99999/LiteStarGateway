"""Dependency wiring for the guardrail policy service.

The keyring is passed because a webhook rule stores a signing secret; the model
repository so a model-scoped rule can be checked against the team's own models.
"""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.application.guardrail_policy_service import GuardrailPolicyService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.guardrail_repository import (
    SQLAlchemyGuardrailRuleRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)


def provide_guardrail_policy_service(
    db_session: NamedDependency[AsyncSession],
    keyring: NamedDependency[Keyring],
    team_service: NamedDependency[TeamService],
) -> GuardrailPolicyService:
    return GuardrailPolicyService(
        SQLAlchemyGuardrailRuleRepository(db_session, keyring),
        team_service,
        SQLAlchemyModelRepository(db_session),
    )
