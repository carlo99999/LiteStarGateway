"""Public login endpoint: email + password -> JWT (valid 7 days)."""

from __future__ import annotations

from litestar import post
from litestar.di import NamedDependency
from litestar.exceptions import NotAuthorizedException
from litestar.status_codes import HTTP_200_OK

from litestar_test.application.user_service import UserService
from litestar_test.domain.exceptions import InvalidCredentials
from litestar_test.infrastructure.web.session.jwt import issue_access_token
from litestar_test.infrastructure.web.session.schemas import LoginRequest, TokenResponse


@post("/login", status_code=HTTP_200_OK)
async def login(
    data: LoginRequest,
    user_service: NamedDependency[UserService],
    jwt_secret: NamedDependency[str],
) -> TokenResponse:
    try:
        user = await user_service.authenticate(data.email, data.password)
    except InvalidCredentials as exc:
        raise NotAuthorizedException("Invalid email or password") from exc

    access_token, expires_in = issue_access_token(str(user.id), jwt_secret, user.token_version)
    return TokenResponse(access_token=access_token, token_type="bearer", expires_in=expires_in)
