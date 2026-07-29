"""The guardrail-rule repository against a real database.

The property worth a DB test is the secret asymmetry: management reads must be
structurally unable to return a signing secret, while the call path — which has
to sign with it — gets it. A comment cannot enforce that; a test can.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import GuardrailKind, GuardrailRule
from litestar_gateway.domain.guardrails import Direction, FailPolicy
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.guardrail_repository import (
    SQLAlchemyGuardrailRuleRepository,
)
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)

TEAM = uuid4()
MODEL = uuid4()
SIGNING_MATERIAL = "webhook-signing-material"  # pragma: allowlist secret
ROTATED_MATERIAL = "rotated-signing-material"  # pragma: allowlist secret


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'guardrails.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def repo(session: AsyncSession) -> SQLAlchemyGuardrailRuleRepository:
    keyring = Keyring(SQLAlchemySecretKeyRepository(session), "salt-key-material", "jwt-secret")
    return SQLAlchemyGuardrailRuleRepository(session, keyring)


def _webhook(name: str, *, model_id=None, position: int = 0) -> GuardrailRule:
    return GuardrailRule(
        id=uuid4(),
        team_id=TEAM,
        model_id=model_id,
        name=name,
        kind=GuardrailKind.WEBHOOK,
        direction=Direction.REQUEST,
        position=position,
        fail_policy=FailPolicy.CLOSED,
        config={"url": "https://scanner.example/check", "timeout_ms": 1500},
    )


async def test_stored_rule_reports_a_secret_without_revealing_it(
    repo: SQLAlchemyGuardrailRuleRepository,
) -> None:
    stored = await repo.add(_webhook("scanner"), secret=SIGNING_MATERIAL)

    assert stored.has_secret is True
    # There is no field on the entity that could carry it — this asserts the
    # shape, which is what keeps a management response from leaking one.
    assert SIGNING_MATERIAL not in repr(stored)
    listed = await repo.list_for_team(TEAM)
    assert SIGNING_MATERIAL not in repr(listed)


async def test_resolve_returns_the_secret_because_it_must_sign_with_it(
    repo: SQLAlchemyGuardrailRuleRepository,
) -> None:
    await repo.add(_webhook("scanner"), secret=SIGNING_MATERIAL)

    active = await repo.resolve(TEAM, MODEL, Direction.REQUEST)

    assert [a.rule.name for a in active] == ["scanner"]
    assert active[0].secret == SIGNING_MATERIAL


async def test_update_without_a_secret_keeps_the_stored_one(
    repo: SQLAlchemyGuardrailRuleRepository,
) -> None:
    stored = await repo.add(_webhook("scanner"), secret=SIGNING_MATERIAL)

    # The realistic edit: an operator changes the timeout. They cannot resubmit
    # a secret they were never shown, so omission must mean "unchanged" —
    # treating it as "clear" would silently unsign the endpoint.
    edited = await repo.update(replace(stored, config={**stored.config, "timeout_ms": 900}))

    assert edited.has_secret is True
    active = await repo.resolve(TEAM, MODEL, Direction.REQUEST)
    assert active[0].secret == SIGNING_MATERIAL
    assert active[0].rule.config["timeout_ms"] == 900


async def test_update_with_a_secret_rotates_it(
    repo: SQLAlchemyGuardrailRuleRepository,
) -> None:
    stored = await repo.add(_webhook("scanner"), secret=SIGNING_MATERIAL)

    await repo.update(stored, secret=ROTATED_MATERIAL)

    active = await repo.resolve(TEAM, MODEL, Direction.REQUEST)
    assert active[0].secret == ROTATED_MATERIAL


async def test_resolve_applies_the_override_and_ordering_rules(
    repo: SQLAlchemyGuardrailRuleRepository,
) -> None:
    await repo.add(_webhook("team-wide", position=0), secret=SIGNING_MATERIAL)
    await repo.add(_webhook("model-first", model_id=MODEL, position=1), secret=SIGNING_MATERIAL)
    await repo.add(_webhook("model-second", model_id=MODEL, position=2), secret=SIGNING_MATERIAL)

    for_model = await repo.resolve(TEAM, MODEL, Direction.REQUEST)
    for_other = await repo.resolve(TEAM, uuid4(), Direction.REQUEST)

    assert [a.rule.name for a in for_model] == ["model-first", "model-second"]
    assert [a.rule.name for a in for_other] == ["team-wide"]


async def test_rules_are_scoped_to_their_team(repo: SQLAlchemyGuardrailRuleRepository) -> None:
    await repo.add(_webhook("scanner"), secret=SIGNING_MATERIAL)
    other_team = uuid4()

    assert await repo.list_for_team(other_team) == []
    assert await repo.resolve(other_team, MODEL, Direction.REQUEST) == []


async def test_get_and_remove_are_team_scoped(repo: SQLAlchemyGuardrailRuleRepository) -> None:
    stored = await repo.add(_webhook("scanner"), secret=SIGNING_MATERIAL)
    other_team = uuid4()

    assert await repo.get(other_team, stored.id) is None
    # A delete addressed with the wrong team must not delete anything, rather
    # than deleting by id and checking the team afterwards.
    assert await repo.remove(other_team, stored.id) is False
    assert await repo.get(TEAM, stored.id) is not None

    assert await repo.remove(TEAM, stored.id) is True
    assert await repo.get(TEAM, stored.id) is None
    assert await repo.remove(TEAM, stored.id) is False
