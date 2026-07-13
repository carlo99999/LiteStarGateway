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
    CredentialInUse,
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
    OrganizationNotEmpty,
    OrganizationNotFound,
    PermissionDenied,
    ProviderMismatch,
    RateLimited,
    RouterNameExists,
    RouterNotFound,
    SaltKeyMissing,
    ScimTokenNotFound,
    ServicePrincipalNotFound,
    SSOEmailNotVerified,
    SSOExchangeError,
    SSOIdentityConflict,
    TeamNotEmpty,
    TeamNotFound,
    UnsupportedNativeField,
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
    (RateLimited, HTTP_429_TOO_MANY_REQUESTS),
    (CredentialNotFound, HTTP_404_NOT_FOUND),
    (ModelNotFound, HTTP_404_NOT_FOUND),
    (RouterNotFound, HTTP_404_NOT_FOUND),
    (AlreadyMember, HTTP_409_CONFLICT),
    (OrganizationNotEmpty, HTTP_409_CONFLICT),
    (TeamNotEmpty, HTTP_409_CONFLICT),
    (LastTeamAdmin, HTTP_409_CONFLICT),
    (EmailAlreadyRegistered, HTTP_409_CONFLICT),
    (CredentialNameExists, HTTP_409_CONFLICT),
    (CredentialInUse, HTTP_409_CONFLICT),
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
    (UnsupportedNativeField, HTTP_400_BAD_REQUEST),
    (UnsupportedOperation, HTTP_501_NOT_IMPLEMENTED),
    (SaltKeyMissing, HTTP_503_SERVICE_UNAVAILABLE),
    (UpstreamRateLimited, HTTP_429_TOO_MANY_REQUESTS),
    (UpstreamTimeout, HTTP_504_GATEWAY_TIMEOUT),
    (UpstreamUnavailable, HTTP_502_BAD_GATEWAY),
    (UpstreamAuthFailed, HTTP_502_BAD_GATEWAY),
    (UpstreamRequestRejected, HTTP_400_BAD_REQUEST),
]


# OpenAI error `type` per HTTP status; 4xx not listed → invalid_request_error,
# 5xx → server_error. Kept in one place so both the shape and the status map
# (`_STATUS`) stay the single source of truth.
_OPENAI_ERROR_TYPE: dict[int, str] = {
    HTTP_400_BAD_REQUEST: "invalid_request_error",
    HTTP_401_UNAUTHORIZED: "authentication_error",
    HTTP_402_PAYMENT_REQUIRED: "insufficient_quota",
    HTTP_403_FORBIDDEN: "permission_error",
    HTTP_404_NOT_FOUND: "not_found_error",
    HTTP_429_TOO_MANY_REQUESTS: "rate_limit_error",
}


def _status_for(exc: DomainError) -> int:
    return next(
        (code for cls, code in _STATUS if isinstance(exc, cls)),
        HTTP_400_BAD_REQUEST,
    )


def _retry_after_headers(exc: DomainError) -> dict[str, str] | None:
    # Forward the provider's Retry-After so client SDK backoff keeps working.
    retry_after = getattr(exc, "retry_after", None)
    if isinstance(retry_after, str):
        return {"Retry-After": retry_after}
    # Our own RPM gate carries an int (seconds until the window resets).
    if isinstance(retry_after, int):
        return {"Retry-After": str(retry_after)}
    return None


def domain_exception_handler(_: Request, exc: DomainError) -> Response:
    detail = str(exc) or exc.__class__.__name__
    return Response(
        {"detail": detail},
        status_code=_status_for(exc),
        headers=_retry_after_headers(exc),
    )


def _openai_error_type(status: int) -> str:
    if status in _OPENAI_ERROR_TYPE:
        return _OPENAI_ERROR_TYPE[status]
    return "server_error" if status >= 500 else "invalid_request_error"


def openai_error_handler(_: Request, exc: DomainError) -> Response:
    """OpenAI-compatible error envelope for the inference surface (`/v1/*`).

    Reuses the same `_STATUS` mapping as `domain_exception_handler`, so the two
    handlers never drift on status; only the body shape differs.
    """
    status = _status_for(exc)
    body = {
        "error": {
            "message": str(exc) or exc.__class__.__name__,
            "type": _openai_error_type(status),
            "code": exc.__class__.__name__,
        }
    }
    return Response(body, status_code=status, headers=_retry_after_headers(exc))
