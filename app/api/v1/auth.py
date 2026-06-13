import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.cookies import clear_auth_cookies, set_auth_cookies
from app.core.errors import ConflictException
from app.core.rate_limit import get_redis
from app.database import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.models.audit import AuditLog
from app.schemas.auth import AuthSuccessResponse, LoginRequest, RegisterRequest
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    exchange_github_code,
    get_github_oauth_url,
    verify_password,
    hash_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

LOCKOUT_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@router.post("/register", response_model=AuthSuccessResponse)
async def register(body: RegisterRequest, response: Response, request: Request, db: AsyncSession = Depends(get_db)):
    # 1. Check if user already exists
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise ConflictException("An account with this email already exists")

    # 2. Slug generation
    base_slug = re.sub(r'[^a-z0-9]+', '-', body.organization_name.lower()).strip('-')
    if not base_slug:
        base_slug = "tenant"
    
    slug = base_slug
    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    if result.scalar_one_or_none():
        slug = f"{base_slug}-{uuid.uuid4().hex[:6]}"

    # 3. Create Tenant
    tenant = Tenant(
        name=body.organization_name,
        slug=slug,
        config={},
        plan_tier="free"
    )
    db.add(tenant)
    await db.flush() # flush to get the tenant id

    # 4. Create User
    user = User(
        tenant_id=tenant.id,
        email=body.email,
        password_hash=hash_password(body.password),
        role="admin"
    )
    db.add(user)
    await db.flush() # flush to get the user id

    # 5. Audit logs
    ip_addr = request.client.host if request.client else None
    audit_tenant = AuditLog(
        tenant_id=tenant.id,
        actor_id=user.id,
        action="tenant.create",
        resource_type="tenant",
        resource_id=tenant.id,
        diff={"name": tenant.name, "slug": tenant.slug},
        ip=ip_addr
    )
    audit_user = AuditLog(
        tenant_id=tenant.id,
        actor_id=user.id,
        action="user.create",
        resource_type="user",
        resource_id=user.id,
        diff={"email": user.email, "role": user.role},
        ip=ip_addr
    )
    db.add_all([audit_tenant, audit_user])

    # 6. Commit transaction
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictException("An account with this email already exists")

    # 7. Generate tokens and set cookies
    access_token = create_access_token({"sub": user.id, "tenant_id": tenant.id})
    refresh_token = create_refresh_token({"sub": user.id, "tenant_id": tenant.id})

    set_auth_cookies(response, access_token, refresh_token)

    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": tenant.id})



@router.post("/login", response_model=AuthSuccessResponse)
async def login(body: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()

    # Lockout check BEFORE credential verification
    if user and user.locked_until and user.locked_until > datetime.now(timezone.utc):
        raise HTTPException(status_code=429, detail="Account locked. Try again later.")

    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        if user:
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= LOCKOUT_ATTEMPTS:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
                logger.warning("Account locked after failed attempts", extra={"email": body.email})
            await db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Success — reset failure counters
    user.failed_attempts = 0
    user.locked_until = None
    await db.commit()

    access_token = create_access_token({"sub": user.id, "tenant_id": user.tenant_id})
    refresh_token = create_refresh_token({"sub": user.id, "tenant_id": user.tenant_id})

    set_auth_cookies(response, access_token, refresh_token)

    # Tokens are in cookies — response body contains only non-sensitive user info
    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": user.tenant_id})


@router.post("/refresh", response_model=AuthSuccessResponse)
async def refresh_token(request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    refresh_tok = request.cookies.get("refresh_token")
    if not refresh_tok:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    payload = decode_token(refresh_tok)
    user_id = payload.get("sub")
    jti = payload.get("jti")

    if not user_id or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    # Check Redis blocklist for revoked tokens
    r = await get_redis() if jti else None
    if jti and await r.get(f"revoked_jti:{jti}"):
        raise HTTPException(status_code=401, detail="Refresh token has been revoked")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Revoke the consumed refresh token (rotation)
    if jti and r:
        ttl = int(payload.get("exp", 0)) - int(datetime.now(timezone.utc).timestamp())
        if ttl > 0:
            await r.setex(f"revoked_jti:{jti}", ttl, "1")

    access_token = create_access_token({"sub": user.id, "tenant_id": user.tenant_id})
    new_refresh_token = create_refresh_token({"sub": user.id, "tenant_id": user.tenant_id})

    set_auth_cookies(response, access_token, new_refresh_token)

    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": user.tenant_id})


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Revoke the refresh token jti in Redis and expire all auth cookies."""
    refresh_tok = request.cookies.get("refresh_token")
    if refresh_tok:
        payload = decode_token(refresh_tok)
        jti = payload.get("jti")
        if jti:
            r = await get_redis()
            ttl = int(payload.get("exp", 0)) - int(datetime.now(timezone.utc).timestamp())
            if ttl > 0:
                await r.setex(f"revoked_jti:{jti}", ttl, "1")

    clear_auth_cookies(response)
    return {"success": True, "message": {"logged_out": True}}


@router.get("/oauth/github/url")
async def github_oauth_url():
    return {"url": get_github_oauth_url()}


@router.get("/me")
async def get_me(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the current user's profile."""
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    return {
        "success": True,
        "data": {
            "user_id": current_user.id,
            "email": current_user.email,
            "role": current_user.role,
            "tenant_id": current_user.tenant_id,
            "tenant_name": tenant.name if tenant else None,
            "tenant_slug": tenant.slug if tenant else None,
            "avatar_url": current_user.avatar_url,
            "oauth_provider": current_user.oauth_provider,
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        },
    }


@router.post("/change-password")
async def change_password(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Change the current user's password."""
    current_pw = body.get("current_password", "")
    new_pw = body.get("new_password", "")

    if not new_pw or len(new_pw) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

    if current_user.password_hash:
        if not verify_password(current_pw, current_user.password_hash):
            raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.password_hash = hash_password(new_pw)
    await db.commit()
    return {"success": True, "message": "Password updated"}


@router.patch("/me")
async def update_profile(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update mutable profile fields (email)."""
    if "email" in body:
        new_email = body["email"].strip().lower()
        existing = (await db.execute(select(User).where(User.email == new_email))).scalar_one_or_none()
        if existing and existing.id != current_user.id:
            raise HTTPException(status_code=400, detail="Email already in use")
        current_user.email = new_email

    if "tenant_name" in body:
        tenant_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
        tenant = tenant_result.scalar_one_or_none()
        if tenant:
            tenant.name = body["tenant_name"].strip()

    await db.commit()
    return {"success": True, "message": "Profile updated"}


@router.post("/oauth/github", response_model=AuthSuccessResponse)
async def github_oauth_callback(
    code: str,
    tenant_id: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # Validate that the tenant actually exists before assigning a user to it
    tenant_result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    if not tenant_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Invalid tenant")

    token_data = await exchange_github_code(code)
    if not token_data or "access_token" not in token_data:
        raise HTTPException(status_code=400, detail="GitHub OAuth failed")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch GitHub user")
        gh_user = resp.json()

    result = await db.execute(
        select(User).where(User.oauth_provider == "github", User.oauth_id == str(gh_user["id"]))
    )
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            tenant_id=tenant_id,
            email=gh_user.get("email", f"{gh_user['login']}@github.local"),
            oauth_provider="github",
            oauth_id=str(gh_user["id"]),
            avatar_url=gh_user.get("avatar_url"),
            role="member",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    access_token = create_access_token({"sub": user.id, "tenant_id": user.tenant_id})
    refresh_token = create_refresh_token({"sub": user.id, "tenant_id": user.tenant_id})

    set_auth_cookies(response, access_token, refresh_token)

    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": user.tenant_id})
