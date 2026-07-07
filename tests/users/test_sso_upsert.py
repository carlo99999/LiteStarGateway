"""UserService.upsert_sso_user: JIT provisioning and the upgrade-only admin rule."""

from __future__ import annotations

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.entities import ExternalIdentity


def _identity(subject: str, *, groups: tuple[str, ...] = ()) -> ExternalIdentity:
    return ExternalIdentity(
        subject=subject, email=f"{subject}@b.com", email_verified=True, groups=groups
    )


async def test_sso_new_user_gets_admin_from_group(service: UserService) -> None:
    user = await service.upsert_sso_user(_identity("s1"), group_admin=True, default_admin=False)
    assert user.is_admin is True


async def test_sso_new_user_gets_admin_from_default_role(service: UserService) -> None:
    # DEFAULT_ROLE=admin seeds a brand-new account even without an admin group.
    user = await service.upsert_sso_user(_identity("s2"), group_admin=False, default_admin=True)
    assert user.is_admin is True


async def test_sso_new_user_defaults_to_member(service: UserService) -> None:
    user = await service.upsert_sso_user(_identity("s3"), group_admin=False, default_admin=False)
    assert user.is_admin is False


async def test_sso_relogin_never_downgrades_admin(service: UserService) -> None:
    # A manual (or prior) admin grant survives a re-login without the admin group —
    # sync is upgrade-only; only the platform-admin endpoint demotes.
    await service.upsert_sso_user(_identity("s4"), group_admin=True, default_admin=False)
    again = await service.upsert_sso_user(_identity("s4"), group_admin=False, default_admin=False)
    assert again.is_admin is True


async def test_sso_relogin_upgrades_member_in_admin_group(service: UserService) -> None:
    await service.upsert_sso_user(_identity("s5"), group_admin=False, default_admin=False)
    again = await service.upsert_sso_user(_identity("s5"), group_admin=True, default_admin=False)
    assert again.is_admin is True
