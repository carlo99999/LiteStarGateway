"""Request/response DTOs for the users web adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar_test.domain.entities import IssuedInvite, IssuedPasswordReset, User


@dataclass(frozen=True)
class SignupRequest:
    invite_token: str
    email: str
    password: str


@dataclass(frozen=True)
class PasswordResetCreateRequest:
    email: str


@dataclass(frozen=True)
class ResetPasswordRequest:
    reset_token: str
    new_password: str


@dataclass(frozen=True)
class UserResponse:
    id: UUID
    email: str
    is_admin: bool
    created_at: datetime

    @classmethod
    def from_entity(cls, user: User) -> UserResponse:
        return cls(
            id=user.id,
            email=user.email,
            is_admin=user.is_admin,
            created_at=user.created_at,
        )


@dataclass(frozen=True)
class InviteResponse:
    """Returned once at creation. `token` is never retrievable again."""

    id: UUID
    token: str
    created_at: datetime

    @classmethod
    def from_issued(cls, issued: IssuedInvite) -> InviteResponse:
        return cls(
            id=issued.invite.id,
            token=issued.token,
            created_at=issued.invite.created_at,
        )


@dataclass(frozen=True)
class PasswordResetResponse:
    """Returned once to the admin. `token` is never retrievable again."""

    token: str
    expires_at: datetime

    @classmethod
    def from_issued(cls, issued: IssuedPasswordReset) -> PasswordResetResponse:
        return cls(token=issued.token, expires_at=issued.reset.expires_at)
