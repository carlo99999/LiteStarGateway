"""R7-M52: check-then-insert paths must translate the unique-constraint race
(two concurrent creates that both pass the service's pre-check) into the domain
409, not an opaque 500. Exercised at the repository boundary by inserting a
duplicate directly — the service pre-check is what these repos assume already
ran, so a second insert of the same unique key hits the constraint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.domain.entities import (
    Credential,
    Model,
    ModelType,
    Provider,
    TeamMembership,
    TeamRole,
)
from litestar_gateway.domain.exceptions import (
    AlreadyMember,
    CredentialNameExists,
    ModelNameExists,
)
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.credential_repository import (
    SQLAlchemyCredentialRepository,
)
from litestar_gateway.infrastructure.persistence.membership_repository import (
    SQLAlchemyTeamMembershipRepository,
)
from litestar_gateway.infrastructure.persistence.model_repository import (
    SQLAlchemyModelRepository,
)
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'races.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _model(team_id, name: str) -> Model:
    return Model(
        id=uuid4(),
        team_id=team_id,
        name=name,
        provider=Provider.OPENAI,
        credential_id=uuid4(),
        type=ModelType.CHAT,
        provider_model_id="gpt-4o",
        params={},
        params_enforced={},
        max_output_tokens=None,
        api_version=None,
        input_cost_per_token=None,
        output_cost_per_token=None,
        enabled=True,
        created_at=datetime.now(UTC),
    )


async def test_duplicate_model_name_raises_domain_error(session: AsyncSession) -> None:
    repo = SQLAlchemyModelRepository(session)
    team_id = uuid4()
    await repo.add(_model(team_id, "dup"))
    with pytest.raises(ModelNameExists):
        await repo.add(_model(team_id, "dup"))


async def test_duplicate_credential_name_raises_domain_error(session: AsyncSession) -> None:
    keyring = Keyring(SQLAlchemySecretKeyRepository(session), "salt-key-material", "jwt-secret")
    repo = SQLAlchemyCredentialRepository(session, keyring)
    values = {"api_key": "x"}

    def _cred() -> Credential:
        return Credential(
            id=uuid4(), name="dup", provider=Provider.OPENAI, created_at=datetime.now(UTC)
        )

    await repo.add(_cred(), values)
    with pytest.raises(CredentialNameExists):
        await repo.add(_cred(), values)


async def test_duplicate_membership_raises_domain_error(session: AsyncSession) -> None:
    repo = SQLAlchemyTeamMembershipRepository(session)
    team_id, user_id = uuid4(), uuid4()

    def _membership() -> TeamMembership:
        return TeamMembership(
            id=uuid4(),
            team_id=team_id,
            user_id=user_id,
            role=TeamRole.MEMBER,
            created_at=datetime.now(UTC),
        )

    await repo.add(_membership())
    with pytest.raises(AlreadyMember):
        await repo.add(_membership())
