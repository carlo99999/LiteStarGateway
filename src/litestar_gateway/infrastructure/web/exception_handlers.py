"""Map domain exceptions to HTTP responses in one place."""

from __future__ import annotations

from litestar import Request, Response
from litestar.status_codes import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_402_PAYMENT_REQUIRED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_501_NOT_IMPLEMENTED,
    HTTP_502_BAD_GATEWAY,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)

from litestar_gateway.domain.exceptions import (
    AlreadyMember,
    APIKeyNotFound,
    BudgetExceeded,
    BudgetNotFound,
    CredentialMisconfigured,
    CredentialNameExists,
    CredentialNotFound,
    DomainError,
    EmailAlreadyRegistered,
    InvalidAPIKey,
    InvalidCredentials,
    InvalidInvite,
    InvalidPasswordReset,
    InvalidRouterConfig,
    InvalidScimToken,
    InvalidServicePrincipal,
    LastTeamAdmin,
    ManagementScopeRequiresServicePrincipal,
    MembershipNotFound,
    ModelDisabled,
    ModelNameExists,
    ModelNotFound,
    ModelTypeMismatch,
    NoRoutableCandidate,
    OrganizationNotFound,
    PermissionDenied,
    ProviderMismatch,
    RouterNameExists,
    RouterNotFound,
    SaltKeyMissing,
    ScimTokenNotFound,
    ServicePrincipalNotFound,
    SSOEmailNotVerified,
    SSOExchangeError,
    SSOIdentityConflict,
    TeamNotFound,
    UnsupportedOperation,
    UpstreamAuthFailed,
    UpstreamRateLimited,
    UpstreamRequestRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
    UserNotFound,
    WeakPassword,
)

# Most specific first; matched by isinstance.
_STATUS: list[tuple[type[DomainError], int]] = [
    (PermissionDenied, HTTP_403_FORBIDDEN),
    (OrganizationNotFound, HTTP_404_NOT_FOUND),
    (TeamNotFound, HTTP_404_NOT_FOUND),
    (UserNotFound, HTTP_404_NOT_FOUND),
    (MembershipNotFound, HTTP_404_NOT_FOUND),
    (APIKeyNotFound, HTTP_404_NOT_FOUND),
    (BudgetNotFound, HTTP_404_NOT_FOUND),
    (ServicePrincipalNotFound, HTTP_404_NOT_FOUND),
    (ScimTokenNotFound, HTTP_404_NOT_FOUND),
    (BudgetExceeded, HTTP_402_PAYMENT_REQUIRED),
    (CredentialNotFound, HTTP_404_NOT_FOUND),
    (ModelNotFound, HTTP_404_NOT_FOUND),
    (RouterNotFound, HTTP_404_NOT_FOUND),
    (AlreadyMember, HTTP_409_CONFLICT),
    (LastTeamAdmin, HTTP_409_CONFLICT),
    (EmailAlreadyRegistered, HTTP_409_CONFLICT),
    (CredentialNameExists, HTTP_409_CONFLICT),
    (ModelNameExists, HTTP_409_CONFLICT),
    (RouterNameExists, HTTP_409_CONFLICT),
    (ModelDisabled, HTTP_409_CONFLICT),
    (InvalidCredentials, HTTP_401_UNAUTHORIZED),
    (InvalidAPIKey, HTTP_401_UNAUTHORIZED),
    (InvalidScimToken, HTTP_401_UNAUTHORIZED),
    (SSOEmailNotVerified, HTTP_401_UNAUTHORIZED),
    (SSOIdentityConflict, HTTP_401_UNAUTHORIZED),
    (SSOExchangeError, HTTP_401_UNAUTHORIZED),
    (InvalidInvite, HTTP_400_BAD_REQUEST),
    (InvalidPasswordReset, HTTP_400_BAD_REQUEST),
    (WeakPassword, HTTP_400_BAD_REQUEST),
    (ProviderMismatch, HTTP_400_BAD_REQUEST),
    (ModelTypeMismatch, HTTP_400_BAD_REQUEST),
    (ManagementScopeRequiresServicePrincipal, HTTP_400_BAD_REQUEST),
    (InvalidServicePrincipal, HTTP_400_BAD_REQUEST),
    (InvalidRouterConfig, HTTP_400_BAD_REQUEST),
    (NoRoutableCandidate, HTTP_400_BAD_REQUEST),
    (CredentialMisconfigured, HTTP_400_BAD_REQUEST),
    (UnsupportedOperation, HTTP_501_NOT_IMPLEMENTED),
    (SaltKeyMissing, HTTP_503_SERVICE_UNAVAILABLE),
    (UpstreamRateLimited, HTTP_429_TOO_MANY_REQUESTS),
    (UpstreamTimeout, HTTP_504_GATEWAY_TIMEOUT),
    (UpstreamUnavailable, HTTP_502_BAD_GATEWAY),
    (UpstreamAuthFailed, HTTP_502_BAD_GATEWAY),
    (UpstreamRequestRejected, HTTP_400_BAD_REQUEST),
]


def domain_exception_handler(_: Request, exc: DomainError) -> Response:
    status = next(
        (code for cls, code in _STATUS if isinstance(exc, cls)),
        HTTP_400_BAD_REQUEST,
    )
    detail = str(exc) or exc.__class__.__name__
    headers: dict[str, str] = {}
    # Forward the provider's Retry-After so client SDK backoff keeps working.
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, str):
        headers["Retry-After"] = retry_after
    return Response({"detail": detail}, status_code=status, headers=headers or None)
