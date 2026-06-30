"""JWT issuing/decoding for login sessions (HS256, 7-day expiry).

Wraps Litestar's `Token`. `decode_access_token` raises `NotAuthorizedException`
(a Litestar HTTP exception) on an invalid or expired token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from litestar.security.jwt import Token

ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(days=7)


def issue_access_token(subject: str, secret: str, token_version: int) -> tuple[str, int]:
    """Return (encoded_jwt, expires_in_seconds) for the given subject (user id).

    Embeds the user's `token_version` so it can be invalidated on logout.
    """
    now = datetime.now(UTC)
    token = Token(sub=subject, iat=now, exp=now + ACCESS_TOKEN_TTL, extras={"tv": token_version})
    encoded = token.encode(secret=secret, algorithm=ALGORITHM)
    return encoded, int(ACCESS_TOKEN_TTL.total_seconds())


def decode_token(encoded_token: str, secret: str) -> tuple[str, int]:
    """Return (subject, token_version) from a valid token, else raise."""
    token = Token.decode(encoded_token=encoded_token, secret=secret, algorithm=ALGORITHM)
    return token.sub, int(token.extras.get("tv", 0))
