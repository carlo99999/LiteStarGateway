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
    PLAYGROUND_EXECUTE = "playground:execute"
    KEYS_READ = "keys:read"
    KEYS_ISSUE = "keys:issue"
    SERVICE_PRINCIPALS_MANAGE = "service-principals:manage"
    USAGE_READ = "usage:read"
    BUDGET_READ = "budget:read"
    # Routing-decision content (raw prompts, §S6 export) — deliberately split
    # from `usage:read`, which only covers token/cost aggregates.
    DECISIONS_READ = "decisions:read"
    # Guardrail policy. Deliberately NOT granted to `model_manager`: a content
    # control that the person configuring models can switch off is not a
    # control. Team admins and platform admins only.
    GUARDRAILS_READ = "guardrails:read"
    GUARDRAILS_MANAGE = "guardrails:manage"
    # MCP tool servers (Plan 20). `TOOLS_MANAGE` is withheld from
    # `model_manager` on the guardrail precedent: attaching a tool server is an
    # egress decision, so it belongs to the team admin. `TOOLS_PROPOSE` is the
    # one permission every role holds, `member` included — a proposal changes no
    # policy until someone with `TOOLS_MANAGE` approves it, so the "a member
    # manages nothing" principle survives.
    TOOLS_READ = "tools:read"
    TOOLS_MANAGE = "tools:manage"
    TOOLS_PROPOSE = "tools:propose"


# The single source of truth for what each team role may do. `admin` holds
# everything; `member` deliberately holds nothing (a member exists to receive
# personal keys and run inference, not to manage the team); the extended roles
# grant exactly one capability domain on top of member.
ROLE_PERMISSIONS: dict[TeamRole, frozenset[Permission]] = {
    TeamRole.ADMIN: frozenset(Permission),
    TeamRole.MEMBER: frozenset({Permission.TOOLS_PROPOSE}),
    TeamRole.MODEL_MANAGER: frozenset(
        {
            Permission.MODELS_READ,
            Permission.MODELS_MANAGE,
            Permission.PLAYGROUND_EXECUTE,
            Permission.DECISIONS_READ,
            Permission.TOOLS_PROPOSE,
        }
    ),
    TeamRole.KEY_ISSUER: frozenset(
        {Permission.KEYS_READ, Permission.KEYS_ISSUE, Permission.TOOLS_PROPOSE}
    ),
    TeamRole.BILLING_VIEWER: frozenset(
        {Permission.USAGE_READ, Permission.BUDGET_READ, Permission.TOOLS_PROPOSE}
    ),
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
