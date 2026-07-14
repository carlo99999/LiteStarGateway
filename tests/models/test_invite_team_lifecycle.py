"""Persistence contracts for the invite/team lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from litestar.testing import AsyncTestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from litestar_gateway.app import create_app
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.application.user_service import UserService
from litestar_gateway.config import Settings
from litestar_gateway.domain.entities import Invite, IssuedInvite, Team, User
from litestar_gateway.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidInvite,
    TeamNotFound,
)
from litestar_gateway.infrastructure.persistence.database import create_database
from litestar_gateway.infrastructure.persistence.invite_repository import (
    SQLAlchemyInviteRepository,
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
from litestar_gateway.infrastructure.persistence.orm import InviteModel
from litestar_gateway.infrastructure.persistence.password_reset_repository import (
    SQLAlchemyPasswordResetRepository,
)
from litestar_gateway.infrastructure.persistence.repository import (
    SQLAlchemyAPIKeyRepository,
)
from litestar_gateway.infrastructure.persistence.team_repository import (
    SQLAlchemyTeamRepository,
)
from litestar_gateway.infrastructure.persistence.user_repository import (
    SQLAlchemyUserRepository,
)


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        admin_email="admin@example.com",
        master_key="master-secret",  # pragma: allowlist secret
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
    )


def _two_party_barrier(
    original: Callable[..., Awaitable[Team | None]],
) -> Callable[..., Awaitable[Team | None]]:
    arrived = 0
    ready = asyncio.Event()

    async def wrapped(*args: object, **kwargs: object) -> Team | None:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return await original(*args, **kwargs)

    return wrapped


def _user_service(session: AsyncSession) -> UserService:
    return UserService(
        users=SQLAlchemyUserRepository(session),
        invites=SQLAlchemyInviteRepository(session),
        password_resets=SQLAlchemyPasswordResetRepository(session),
        transaction=session,
        teams=SQLAlchemyTeamRepository(session),
        memberships=SQLAlchemyTeamMembershipRepository(session),
    )


def _team_service(session: AsyncSession) -> TeamService:
    return TeamService(
        organizations=SQLAlchemyOrganizationRepository(session),
        teams=SQLAlchemyTeamRepository(session),
        memberships=SQLAlchemyTeamMembershipRepository(session),
        users=SQLAlchemyUserRepository(session),
        transaction=session,
        models=SQLAlchemyModelRepository(session),
        api_keys=SQLAlchemyAPIKeyRepository(session),
    )


async def test_invite_add_translates_stale_team_fk_and_rolls_back(database_url: str) -> None:
    settings = _settings(database_url)
    async with AsyncTestClient(app=create_app(settings)):
        engine = create_database(settings).config.get_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        team_id = uuid4()
        now = datetime.now(UTC)
        invite = Invite(
            id=uuid4(),
            token_hash="not-a-secret",  # pragma: allowlist secret
            created_at=now,
            expires_at=now + timedelta(hours=1),
            used_at=None,
            team_id=team_id,
            role="member",
        )
        try:
            async with sessions() as session:
                with pytest.raises(TeamNotFound, match=str(team_id)):
                    await SQLAlchemyInviteRepository(session).add(invite)
                await session.rollback()
                count = await session.scalar(select(func.count()).select_from(InviteModel))
                assert count == 0
        finally:
            await engine.dispose()


async def test_concurrent_invite_create_and_team_delete_are_linearized(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(database_url)
    async with AsyncTestClient(app=create_app(settings)) as client:
        login = await client.post(
            "/login",
            json={"email": settings.admin_email, "password": settings.master_key},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        organization = await client.post(
            "/organizations", json={"name": "Invite Race"}, headers=headers
        )
        team = await client.post(
            f"/organizations/{organization.json()['id']}/teams",
            json={"name": "Race Team", "admin_email": settings.admin_email},
            headers=headers,
        )
        team_id = UUID(team.json()["id"])

        engine = create_database(settings).config.get_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as actor_session:
                actor = await SQLAlchemyUserRepository(actor_session).get_by_email(
                    settings.admin_email
                )
                assert actor is not None

            monkeypatch.setattr(
                SQLAlchemyTeamRepository,
                "lock_for_lifecycle",
                _two_party_barrier(SQLAlchemyTeamRepository.lock_for_lifecycle),
            )
            async with sessions() as create_session, sessions() as delete_session:
                created, deleted = await asyncio.wait_for(
                    asyncio.gather(
                        _user_service(create_session).create_invite(team_id=team_id),
                        _team_service(delete_session).delete_team(actor, team_id),
                        return_exceptions=True,
                    ),
                    timeout=10,
                )
            monkeypatch.undo()

            assert isinstance(deleted, Team)
            assert isinstance(created, IssuedInvite | TeamNotFound)
            async with sessions() as verify_session:
                assert await SQLAlchemyTeamRepository(verify_session).get(team_id) is None
                invite_count = await verify_session.scalar(
                    select(func.count())
                    .select_from(InviteModel)
                    .where(InviteModel.team_id == team_id)
                )
                assert invite_count == 0
        finally:
            monkeypatch.undo()
            await engine.dispose()


async def test_concurrent_invite_redemption_and_team_delete_are_linearized(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not database_url.startswith("postgresql"):
        pytest.skip("concurrent redemption lock semantics require Postgres")
    settings = _settings(database_url)
    async with AsyncTestClient(app=create_app(settings)) as client:
        login = await client.post(
            "/login",
            json={"email": settings.admin_email, "password": settings.master_key},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        organization = await client.post(
            "/organizations", json={"name": "Redemption Race"}, headers=headers
        )
        team = await client.post(
            f"/organizations/{organization.json()['id']}/teams",
            json={"name": "Signup Team", "admin_email": settings.admin_email},
            headers=headers,
        )
        team_id = UUID(team.json()["id"])
        invite = await client.post(
            "/invites",
            json={"team_id": str(team_id), "role": "member"},
            headers=headers,
        )

        engine = create_database(settings).config.get_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as actor_session:
                actor = await SQLAlchemyUserRepository(actor_session).get_by_email(
                    settings.admin_email
                )
                assert actor is not None

            monkeypatch.setattr(
                SQLAlchemyTeamRepository,
                "lock_for_lifecycle",
                _two_party_barrier(SQLAlchemyTeamRepository.lock_for_lifecycle),
            )
            async with sessions() as register_session, sessions() as delete_session:
                registered, deleted = await asyncio.wait_for(
                    asyncio.gather(
                        _user_service(register_session).register(
                            invite.json()["token"],
                            "racer@example.com",
                            "Passw0rd!",
                        ),
                        _team_service(delete_session).delete_team(actor, team_id),
                        return_exceptions=True,
                    ),
                    timeout=15,
                )
            monkeypatch.undo()

            assert isinstance(deleted, Team)
            assert isinstance(registered, User | InvalidInvite)
            async with sessions() as verify_session:
                racer = await SQLAlchemyUserRepository(verify_session).get_by_email(
                    "racer@example.com"
                )
            assert (racer is not None) is isinstance(registered, User)
        finally:
            monkeypatch.undo()
            await engine.dispose()


async def test_invite_expiry_is_rechecked_by_atomic_consumption(database_url: str) -> None:
    settings = _settings(database_url)
    async with AsyncTestClient(app=create_app(settings)) as client:
        login = await client.post(
            "/login",
            json={"email": settings.admin_email, "password": settings.master_key},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        organization = await client.post(
            "/organizations", json={"name": "Expiry Org"}, headers=headers
        )
        team = await client.post(
            f"/organizations/{organization.json()['id']}/teams",
            json={"name": "Expiry Team", "admin_email": settings.admin_email},
            headers=headers,
        )
        invite = await client.post(
            "/invites",
            json={"team_id": team.json()["id"], "role": "member"},
            headers=headers,
        )

        engine = create_database(settings).config.get_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            invite_id = UUID(invite.json()["id"])
            now = datetime.now(UTC)
            async with sessions() as session:
                await session.execute(
                    update(InviteModel)
                    .where(InviteModel.id == invite_id)
                    .values(expires_at=now - timedelta(seconds=1))
                )
                await session.commit()
                assert not await SQLAlchemyInviteRepository(session).mark_used(invite_id, now)
                await session.commit()
                stored = await session.get(InviteModel, invite_id)
                assert stored is not None
                assert stored.used_at is None
        finally:
            await engine.dispose()


async def test_concurrent_duplicate_email_consumes_both_invites(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not database_url.startswith("postgresql"):
        pytest.skip("concurrent uniqueness arbitration requires Postgres")
    settings = _settings(database_url)
    async with AsyncTestClient(app=create_app(settings)) as client:
        login = await client.post(
            "/login",
            json={"email": settings.admin_email, "password": settings.master_key},
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        organization = await client.post(
            "/organizations", json={"name": "Email Race"}, headers=headers
        )
        invite_responses = []
        for suffix in ("A", "B"):
            team = await client.post(
                f"/organizations/{organization.json()['id']}/teams",
                json={"name": f"Email Team {suffix}", "admin_email": settings.admin_email},
                headers=headers,
            )
            invite_responses.append(
                await client.post(
                    "/invites",
                    json={"team_id": team.json()["id"], "role": "member"},
                    headers=headers,
                )
            )

        engine = create_database(settings).config.get_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        original_add = SQLAlchemyUserRepository.add_staged
        arrived = 0
        ready = asyncio.Event()

        async def synchronized_add(repository: SQLAlchemyUserRepository, user: User) -> User:
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                ready.set()
            await ready.wait()
            return await original_add(repository, user)

        try:
            monkeypatch.setattr(SQLAlchemyUserRepository, "add_staged", synchronized_add)
            async with sessions() as first_session, sessions() as second_session:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        _user_service(first_session).register(
                            invite_responses[0].json()["token"],
                            "same@example.com",
                            "Passw0rd!",
                        ),
                        _user_service(second_session).register(
                            invite_responses[1].json()["token"],
                            "same@example.com",
                            "Passw0rd!",
                        ),
                        return_exceptions=True,
                    ),
                    timeout=15,
                )
            monkeypatch.undo()

            assert sum(isinstance(result, User) for result in results) == 1
            assert sum(isinstance(result, EmailAlreadyRegistered) for result in results) == 1
            invite_ids = [UUID(response.json()["id"]) for response in invite_responses]
            async with sessions() as verify_session:
                used_at = (
                    await verify_session.scalars(
                        select(InviteModel.used_at).where(InviteModel.id.in_(invite_ids))
                    )
                ).all()
                stored_user = await SQLAlchemyUserRepository(verify_session).get_by_email(
                    "same@example.com"
                )
            assert len(used_at) == 2
            assert all(value is not None for value in used_at)
            assert stored_user is not None
        finally:
            monkeypatch.undo()
            await engine.dispose()
