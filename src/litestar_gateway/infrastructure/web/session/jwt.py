"""JWT issuing/decoding for login sessions (HS256, 7-day expiry).

Wraps Litestar's `Token`. Tokens are signed with the keyring's active JWT key and
verified against any usable key (so daily key rotation doesn't invalidate tokens
still within their lifetime). `decode_token` raises `NotAuthorizedException` when
no key verifies the token.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from litestar.exceptions import NotAuthorizedException
from litestar.security.jwt import Token

ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(days=7)
BEARER_TOKEN_KIND = "bearer"
BROWSER_SESSION_KIND = "browser_session"


def issue_access_token(subject: str, secret: str, token_version: int) -> tuple[str, int]:
    """Return (encoded_jwt, expires_in_seconds) for the given subject (user id).

    Embeds the user's `token_version` so it can be invalidated on logout.
    """
    now = datetime.now(UTC)
    token = Token(
        sub=subject,
        iat=now,
        exp=now + ACCESS_TOKEN_TTL,
        extras={"tv": token_version, "kind": BEARER_TOKEN_KIND},
    )
    encoded = token.encode(secret=secret, algorithm=ALGORITHM)
    return encoded, int(ACCESS_TOKEN_TTL.total_seconds())


def decode_token(encoded_token: str, secrets: Sequence[str]) -> tuple[str, int]:
    """Return (subject, token_version) from a token that verifies against any of
    the given keyring secrets, else raise NotAuthorizedException."""
    for secret in secrets:
        try:
            token = Token.decode(encoded_token=encoded_token, secret=secret, algorithm=ALGORITHM)
        except NotAuthorizedException:
            continue
        # Tokens minted before transport isolation did not carry a kind. Keep
        # those valid on Authorization during the normal seven-day migration
        # window, but never accept browser-session tokens as bearer tokens.
        if token.extras.get("kind") not in (None, BEARER_TOKEN_KIND):
            raise NotAuthorizedException("Invalid bearer token")
        return token.sub, int(token.extras.get("tv", 0))
    raise NotAuthorizedException("Invalid or expired token")


def issue_browser_session(
    subject: str, secret: str, token_version: int, csrf_token: str
) -> tuple[str, int]:
    """Mint a cookie-only JWT bound to an in-memory CSRF secret."""
    now = datetime.now(UTC)
    token = Token(
        sub=subject,
        iat=now,
        exp=now + ACCESS_TOKEN_TTL,
        extras={
            "tv": token_version,
            "kind": BROWSER_SESSION_KIND,
            "csrf": csrf_token,
        },
    )
    encoded = token.encode(secret=secret, algorithm=ALGORITHM)
    return encoded, int(ACCESS_TOKEN_TTL.total_seconds())


def decode_browser_session(encoded_token: str, secrets: Sequence[str]) -> tuple[str, int, str]:
    """Decode only browser-session JWTs; bearer JWTs are deliberately rejected."""
    for secret in secrets:
        try:
            token = Token.decode(encoded_token=encoded_token, secret=secret, algorithm=ALGORITHM)
        except NotAuthorizedException:
            continue
        csrf_token = token.extras.get("csrf")
        if token.extras.get("kind") != BROWSER_SESSION_KIND or not isinstance(csrf_token, str):
            raise NotAuthorizedException("Invalid browser session")
        if not csrf_token:
            raise NotAuthorizedException("Invalid browser session")
        return token.sub, int(token.extras.get("tv", 0)), csrf_token
    raise NotAuthorizedException("Invalid or expired browser session")
