"""Request/response DTOs for the users web adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_gateway.domain.entities import IssuedInvite, IssuedPasswordReset, TeamRole, User


@dataclass(frozen=True)
class SignupRequest:
    invite_token: str
    email: str
    password: str


@dataclass(frozen=True)
class InviteCreateRequest:
    """Admin picks the team the invited user joins and their role there — both
    required, so a new account is never created team-less. `role` is validated
    against the TeamRole enum by the framework."""

    team_id: UUID
    role: TeamRole = TeamRole.MEMBER


@dataclass(frozen=True)
class PasswordResetCreateRequest:
    email: str


@dataclass(frozen=True)
class ResetPasswordRequest:
    reset_token: str
    new_password: str


@dataclass(frozen=True)
class SetUserActiveRequest:
    is_active: bool


@dataclass(frozen=True)
class SetUserAdminRequest:
    is_admin: bool


@dataclass(frozen=True)
class SetUserAuditorRequest:
    is_auditor: bool


@dataclass(frozen=True)
class UserResponse:
    id: UUID
    email: str
    is_admin: bool
    is_auditor: bool
    is_active: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, user: User) -> UserResponse:
        return cls(
            id=user.id,
            email=user.email,
            is_admin=user.is_admin,
            is_auditor=user.is_auditor,
            is_active=user.is_active,
            created_at=user.created_at,
        )


@dataclass(frozen=True)
class InviteResponse:
    """Returned once at creation. `token` is never retrievable again."""

    id: UUID
    token: str
    created_at: datetime
    expires_at: datetime
    team_id: UUID | None
    role: str | None

    @classmethod
    def from_issued(cls, issued: IssuedInvite) -> InviteResponse:
        return cls(
            id=issued.invite.id,
            token=issued.token,
            created_at=issued.invite.created_at,
            expires_at=issued.invite.expires_at,
            team_id=issued.invite.team_id,
            role=issued.invite.role,
        )


@dataclass(frozen=True)
class PasswordResetResponse:
    """Returned once to the admin. `token` is never retrievable again."""

    token: str
    expires_at: datetime

    @classmethod
    def from_issued(cls, issued: IssuedPasswordReset) -> PasswordResetResponse:
        return cls(token=issued.token, expires_at=issued.reset.expires_at)
