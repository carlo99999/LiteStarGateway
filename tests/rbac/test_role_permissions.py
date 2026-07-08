"""Pure domain mapping: role → permission set, and SSO_TEAM_MAPPING extended roles."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from litestar_gateway.config import TeamGrant, _env_team_mapping
from litestar_gateway.domain.authorization import (
    AUDITOR_TEAM_PERMISSIONS,
    ROLE_PERMISSIONS,
    Permission,
)
from litestar_gateway.domain.entities import TeamRole


def test_every_team_role_has_a_permission_set() -> None:
    assert set(ROLE_PERMISSIONS) == set(TeamRole)


def test_admin_holds_every_permission_and_member_none() -> None:
    assert ROLE_PERMISSIONS[TeamRole.ADMIN] == frozenset(Permission)
    assert ROLE_PERMISSIONS[TeamRole.MEMBER] == frozenset()


def test_new_roles_are_scoped_to_their_domain() -> None:
    assert ROLE_PERMISSIONS[TeamRole.MODEL_MANAGER] == frozenset(
        {Permission.MODELS_READ, Permission.MODELS_MANAGE, Permission.DECISIONS_READ}
    )
    assert ROLE_PERMISSIONS[TeamRole.KEY_ISSUER] == frozenset(
        {Permission.KEYS_READ, Permission.KEYS_ISSUE}
    )
    assert ROLE_PERMISSIONS[TeamRole.BILLING_VIEWER] == frozenset(
        {Permission.USAGE_READ, Permission.BUDGET_READ}
    )


def test_auditor_bypass_is_read_only() -> None:
    assert AUDITOR_TEAM_PERMISSIONS == frozenset({Permission.USAGE_READ, Permission.BUDGET_READ})
    # R6-C2: never raw prompt content via the cross-team bypass.
    assert Permission.DECISIONS_READ not in AUDITOR_TEAM_PERMISSIONS


def test_sso_team_mapping_accepts_extended_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    team_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setenv(
        "SSO_TEAM_MAPPING",
        json.dumps({"ml-eng": [{"team": team_id, "role": "model-manager"}]}),
    )
    mapping = _env_team_mapping("SSO_TEAM_MAPPING")
    assert mapping["ml-eng"] == (TeamGrant(UUID(team_id), TeamRole.MODEL_MANAGER),)
