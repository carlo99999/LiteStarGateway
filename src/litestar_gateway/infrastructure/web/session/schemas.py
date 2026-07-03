"""DTOs for login."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LoginRequest:
    email: str
    password: str


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    token_type: str
    expires_in: int  # seconds
