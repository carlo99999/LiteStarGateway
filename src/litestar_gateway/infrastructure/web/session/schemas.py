"""DTOs for login."""

from __future__ import annotations

from dataclasses import dataclass

from litestar_gateway.infrastructure.web.users.schemas import UserResponse


@dataclass(frozen=True)
class LoginRequest:
    email: str
    password: str


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    token_type: str
    expires_in: int  # seconds


@dataclass(frozen=True)
class BrowserSessionResponse:
    user: UserResponse
    csrf_token: str
    expires_in: int  # seconds
