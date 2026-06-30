"""Map domain exceptions to HTTP responses in one place."""

from __future__ import annotations

from litestar import Request, Response
from litestar.status_codes import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_501_NOT_IMPLEMENTED,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from litestar_test.domain.exceptions import (
    AlreadyMember,
    APIKeyNotFound,
    CredentialMisconfigured,
    CredentialNameExists,
    CredentialNotFound,
    DomainError,
    EmailAlreadyRegistered,
    InvalidAPIKey,
    InvalidCredentials,
    InvalidInvite,
    MembershipNotFound,
    ModelDisabled,
    ModelNameExists,
    ModelNotFound,
    ModelTypeMismatch,
    OrganizationNotFound,
    PermissionDenied,
    ProviderMismatch,
    SaltKeyMissing,
    TeamNotFound,
    UnsupportedOperation,
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
    (CredentialNotFound, HTTP_404_NOT_FOUND),
    (ModelNotFound, HTTP_404_NOT_FOUND),
    (AlreadyMember, HTTP_409_CONFLICT),
    (EmailAlreadyRegistered, HTTP_409_CONFLICT),
    (CredentialNameExists, HTTP_409_CONFLICT),
    (ModelNameExists, HTTP_409_CONFLICT),
    (ModelDisabled, HTTP_409_CONFLICT),
    (InvalidCredentials, HTTP_401_UNAUTHORIZED),
    (InvalidAPIKey, HTTP_401_UNAUTHORIZED),
    (InvalidInvite, HTTP_400_BAD_REQUEST),
    (WeakPassword, HTTP_400_BAD_REQUEST),
    (ProviderMismatch, HTTP_400_BAD_REQUEST),
    (ModelTypeMismatch, HTTP_400_BAD_REQUEST),
    (CredentialMisconfigured, HTTP_400_BAD_REQUEST),
    (UnsupportedOperation, HTTP_501_NOT_IMPLEMENTED),
    (SaltKeyMissing, HTTP_503_SERVICE_UNAVAILABLE),
]


def domain_exception_handler(_: Request, exc: DomainError) -> Response:
    status = next(
        (code for cls, code in _STATUS if isinstance(exc, cls)),
        HTTP_400_BAD_REQUEST,
    )
    detail = str(exc) or exc.__class__.__name__
    return Response({"detail": detail}, status_code=status)
