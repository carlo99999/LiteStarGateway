"""Extended RBAC: the declarative role → permission model.

One place answers "what may this role do": services enforce permissions through
`TeamService.ensure_team_permission` (and its Principal variant), which consult
the mapping below — never by re-checking role names inline. Platform admins
bypass every check; the platform auditor bypasses only the read-only
`AUDITOR_TEAM_PERMISSIONS` subset (plus the /audit read surface).
"""

from __future__ import annotations

from enum import StrEnum

from litestar_gateway.domain.entities import TeamRole


class Permission(StrEnum):
    """Team-scoped capabilities enforced by the management API."""

    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    MODELS_READ = "models:read"
    MODELS_MANAGE = "models:manage"
    KEYS_READ = "keys:read"
    KEYS_ISSUE = "keys:issue"
    SERVICE_PRINCIPALS_MANAGE = "service-principals:manage"
    USAGE_READ = "usage:read"
    BUDGET_READ = "budget:read"
    # Routing-decision content (raw prompts, §S6 export) — deliberately split
    # from `usage:read`, which only covers token/cost aggregates.
    DECISIONS_READ = "decisions:read"


# The single source of truth for what each team role may do. `admin` holds
# everything; `member` deliberately holds nothing (a member exists to receive
# personal keys and run inference, not to manage the team); the extended roles
# grant exactly one capability domain on top of member.
ROLE_PERMISSIONS: dict[TeamRole, frozenset[Permission]] = {
    TeamRole.ADMIN: frozenset(Permission),
    TeamRole.MEMBER: frozenset(),
    TeamRole.MODEL_MANAGER: frozenset(
        {Permission.MODELS_READ, Permission.MODELS_MANAGE, Permission.DECISIONS_READ}
    ),
    TeamRole.KEY_ISSUER: frozenset({Permission.KEYS_READ, Permission.KEYS_ISSUE}),
    TeamRole.BILLING_VIEWER: frozenset({Permission.USAGE_READ, Permission.BUDGET_READ}),
}

# What a platform auditor (User.is_auditor) may do in ANY team without being a
# member: strictly read-only billing visibility. Mutating permissions are never
# granted this way, and neither is `decisions:read` — decision exports carry
# raw end-user prompts, not billing aggregates.
AUDITOR_TEAM_PERMISSIONS: frozenset[Permission] = frozenset(
    {Permission.USAGE_READ, Permission.BUDGET_READ}
)


def role_grants(role: TeamRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
