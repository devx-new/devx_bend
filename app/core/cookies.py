import os
import secrets

from fastapi import Response

ACCESS_TOKEN_COOKIE = "access_token"
REFRESH_TOKEN_COOKIE = "refresh_token"
CSRF_TOKEN_COOKIE = "csrf_token"

_REFRESH_PATH = "/v1/auth"


def _cookie_base() -> dict:
    secure = os.environ.get("cookie_secure", "false").lower() in ("true", "1")
    samesite = os.environ.get("cookie_samesite", "lax")
    return {"secure": secure, "samesite": samesite, "domain": None}


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    from app.config import settings
    base = _cookie_base()

    response.set_cookie(
        ACCESS_TOKEN_COOKIE,
        access_token,
        httponly=True,
        path="/",
        max_age=settings.access_token_expire_minutes * 60,
        **base,
    )

    response.set_cookie(
        REFRESH_TOKEN_COOKIE,
        refresh_token,
        httponly=True,
        path=_REFRESH_PATH,
        max_age=settings.refresh_token_expire_days * 86_400,
        **base,
    )

    csrf = secrets.token_hex(32)
    response.set_cookie(
        CSRF_TOKEN_COOKIE,
        csrf,
        httponly=False,
        path="/",
        max_age=settings.csrf_token_expire_minutes * 60,
        **base,
    )


def clear_auth_cookies(response: Response) -> None:
    base = _cookie_base()
    for name, path in (
        (ACCESS_TOKEN_COOKIE, "/"),
        (REFRESH_TOKEN_COOKIE, _REFRESH_PATH),
        (CSRF_TOKEN_COOKIE, "/"),
    ):
        response.delete_cookie(
            name,
            path=path,
            secure=base["secure"],
            samesite=base["samesite"],
        )
