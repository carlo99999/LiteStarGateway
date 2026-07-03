"""Public signup endpoint — requires a valid single-use invite token."""

from __future__ import annotations

from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException

from litestar_gateway.application.user_service import UserService
from litestar_gateway.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidInvite,
    WeakPassword,
)
from litestar_gateway.infrastructure.web.rate_limit import build_auth_rate_limit
from litestar_gateway.infrastructure.web.users.schemas import SignupRequest, UserResponse


@post("/signup", middleware=[build_auth_rate_limit().middleware])
async def signup(data: SignupRequest, user_service: NamedDependency[UserService]) -> UserResponse:
    try:
        user = await user_service.register(
            invite_token=data.invite_token,
            email=data.email,
            password=data.password,
        )
    except (InvalidInvite, WeakPassword) as exc:
        raise ClientException(str(exc)) from exc
    except EmailAlreadyRegistered as exc:
        # Non-revealing response: do not disclose that the email already exists
        # (avoids account enumeration). The detailed reason is kept server-side
        # via the chained exception.
        raise ClientException("Unable to complete sign up.") from exc
    return UserResponse.from_entity(user)
