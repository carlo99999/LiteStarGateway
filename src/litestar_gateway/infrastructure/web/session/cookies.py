"""Hardened host-only cookies for browser admin sessions."""

from __future__ import annotations

from litestar import Request
from litestar.datastructures import Cookie

LOCAL_SESSION_COOKIE = "lsg_session"
SECURE_SESSION_COOKIE = "__Host-lsg_session"


def browser_session_is_secure(request: Request, *, configured: bool) -> bool:
    """Require Secure when configured or when Litestar sees an HTTPS request."""
    return configured or request.url.scheme == "https"


def browser_session_cookie_name(*, secure: bool) -> str:
    return SECURE_SESSION_COOKIE if secure else LOCAL_SESSION_COOKIE


def browser_session_cookie(value: str, *, secure: bool, max_age: int) -> Cookie:
    return Cookie(
        key=browser_session_cookie_name(secure=secure),
        value=value,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )


def expired_browser_session_cookie(*, secure: bool) -> Cookie:
    return browser_session_cookie("", secure=secure, max_age=0)
