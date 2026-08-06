import hmac

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cookies import ACCESS_TOKEN_COOKIE, CSRF_TOKEN_COOKIE
from app.database import get_db
from app.models.user import User
from app.security import decode_token


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    # Prefer Authorization: Bearer <token> (works cross-origin without cookie restrictions).
    # Fall back to the HttpOnly access_token cookie for same-origin / proxy setups.
    token: str | None = None
    using_bearer = False

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        using_bearer = True
    else:
        token = request.cookies.get(ACCESS_TOKEN_COOKIE)

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Double-submit CSRF check on mutating methods — only for cookie auth.
    # Bearer token requests are already protected by the token itself.
    if not using_bearer and request.method not in ("GET", "HEAD", "OPTIONS"):
        csrf_cookie = request.cookies.get(CSRF_TOKEN_COOKIE, "")
        csrf_header = request.headers.get("X-CSRF-Token", "")
        if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(status_code=403, detail="CSRF token mismatch")

    payload = decode_token(token)
    user_id = payload.get("sub")
    if not user_id or payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Account not found")

    tenant_id = payload.get("tenant_id")
    if not tenant_id or tenant_id != user.tenant_id:
        raise HTTPException(status_code=401, detail="Account not found in organization")
    return user


def require_role(role: str):
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in ("admin", role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return role_checker


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Allows only company admins. Super admins are internal and excluded."""
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Company admin access required")
    return current_user


async def require_verified_email(current_user: User = Depends(get_current_user)) -> User:
    """Gate for actions with outward-facing side effects (inviting members, connecting
    third-party integrations, creating routing rules) — requires a verified email."""
    if not current_user.is_verified:
        raise HTTPException(status_code=403, detail="Please verify your email address to do this")
    return current_user


async def require_super_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "super_admin":
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return current_user
