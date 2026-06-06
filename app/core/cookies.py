"""
Cookie management helpers for HttpOnly JWT authentication.

All auth cookie set/clear logic lives here so attributes are consistent
across login, refresh, logout, and OAuth endpoints.

Cookie layout:
  access_token  — HttpOnly, SameSite=Lax, Path=/,         Max-Age=15min
  refresh_token — HttpOnly, SameSite=Lax, Path=/v1/auth,  Max-Age=7days
  csrf_token    — NOT HttpOnly (JS must read it),          Max-Age=15min
"""

import secrets

from fastapi import Response

from app.config import settings

ACCESS_TOKEN_COOKIE = "access_token"
REFRESH_TOKEN_COOKIE = "refresh_token"
CSRF_TOKEN_COOKIE = "csrf_token"

# Scoped path for the refresh token — sent only to /v1/auth/* endpoints
_REFRESH_PATH = "/v1/auth"


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    """
    Write all three auth cookies onto the response.

    access_token  — the short-lived JWT; HttpOnly prevents JS access.
    refresh_token — the long-lived JWT; scoped to /v1/auth so the browser
                    only sends it to refresh/logout, never to data endpoints.
    csrf_token    — a random opaque value; NOT HttpOnly so the frontend JS
                    can read it and echo it in the X-CSRF-Token header.
    """
    _base = dict(
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        domain=None,  # scope to current host (localhost in dev)
    )

    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        httponly=True,
        path="/",
        max_age=settings.access_token_expire_minutes * 60,
        **_base,
    )

    response.set_cookie(
        REFRESH_TOKEN_COOKIE,
        refresh_token,
        httponly=True,
        path=_REFRESH_PATH,
        max_age=settings.refresh_token_expire_days * 86_400,
        **_base,
    )

    csrf = secrets.token_hex(32)
    response.set_cookie(
        CSRF_TOKEN_COOKIE,
        csrf,
        httponly=False,  # JS-readable — intentional for double-submit CSRF pattern
        path="/",
        max_age=settings.csrf_token_expire_minutes * 60,
        **_base,
    )


def clear_auth_cookies(response: Response) -> None:
    """Expire all three auth cookies (call on logout)."""
    for name, path in (
        (ACCESS_TOKEN_COOKIE, "/"),
        (REFRESH_TOKEN_COOKIE, _REFRESH_PATH),
        (CSRF_TOKEN_COOKIE, "/"),
    ):
        response.delete_cookie(name, path=path)
