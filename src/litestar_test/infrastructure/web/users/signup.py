"""Public signup endpoint — requires a valid single-use invite token."""

from __future__ import annotations

from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import ClientException
from litestar.status_codes import HTTP_409_CONFLICT

from litestar_test.application.user_service import UserService
from litestar_test.domain.exceptions import (
    EmailAlreadyRegistered,
    InvalidInvite,
    WeakPassword,
)
from litestar_test.infrastructure.web.users.schemas import SignupRequest, UserResponse


@post("/signup")
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
        raise ClientException(str(exc), status_code=HTTP_409_CONFLICT) from exc
    return UserResponse.from_entity(user)
