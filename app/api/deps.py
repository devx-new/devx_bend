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
    token = request.cookies.get(ACCESS_TOKEN_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Double-submit CSRF check on all state-mutating methods.
    # GET/HEAD/OPTIONS are safe methods (no side effects) — exempt from CSRF.
    # WebSocket upgrades are also exempt (no custom headers possible in browsers).
    if request.method not in ("GET", "HEAD", "OPTIONS"):
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
        raise HTTPException(status_code=401, detail="User not found")

    tenant_id = payload.get("tenant_id")
    if not tenant_id or tenant_id != user.tenant_id:
        raise HTTPException(status_code=401, detail="User not found in organization")
    return user


def require_role(role: str):
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in ("admin", role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return role_checker
