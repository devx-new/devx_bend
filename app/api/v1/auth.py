import logging
import re
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role, require_verified_email
from app.config import settings
from app.core.cache import TEAM_MEMBERS_TTL, cache_del, cache_del_pattern, cache_get, cache_set
from app.core.cookies import clear_auth_cookies, set_auth_cookies
from app.core.errors import ConflictException
from app.core.rate_limit import check_rate_limit, get_redis
from app.core.tokens import (
    consume_email_verification_token,
    consume_password_reset_token,
    create_email_verification_token,
    create_password_reset_token,
)
from app.database import get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.models.audit import AuditLog
from app.schemas.auth import (
    AuthSuccessResponse,
    ForgotPasswordRequest,
    InviteMemberRequest,
    InviteMemberResponse,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    VerifyEmailRequest,
)
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    exchange_github_code,
    get_github_oauth_url,
    verify_password,
    hash_password,
)
from app.worker.email_tasks import send_templated_email_task

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*"


def _generate_password(length: int = 16) -> str:
    """Generate a cryptographically secure random password."""
    while True:
        pw = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        # Ensure at least one of each character class
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%^&*" for c in pw)):
            return pw

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

LOCKOUT_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


async def _queue_verification_email(user: User) -> None:
    """Generate a one-time verification token and queue the email via Celery. Never raises."""
    try:
        token = await create_email_verification_token(user.id)
        verify_url = f"{settings.frontend_url}/verify-email?token={token}"
        async_result = send_templated_email_task.delay(
            user.email,
            "Verify your email address",
            "generic.html",
            heading="Verify your email address",
            body_paragraphs=[
                "Thanks for signing up for DevX! Please confirm your email address to finish setting up your account.",
                "This link expires in 24 hours.",
            ],
            button_url=verify_url,
            button_text="Verify email",
        )
        logger.info("Queued verification email", extra={"user_id": user.id, "task_id": async_result.id})
    except Exception:
        logger.exception("Failed to queue verification email", extra={"user_id": user.id})


async def _queue_password_reset_email(user: User) -> None:
    """Generate a one-time reset token and queue the email via Celery. Never raises."""
    try:
        token = await create_password_reset_token(user.id)
        reset_url = f"{settings.frontend_url}/reset-password?token={token}"
        async_result = send_templated_email_task.delay(
            user.email,
            "Reset your password",
            "generic.html",
            heading="Reset your password",
            body_paragraphs=[
                "We received a request to reset your DevX password. Click the button below to choose a new one.",
                "If you didn't request this, you can safely ignore this email — your password won't be changed.",
                "This link expires in 1 hour.",
            ],
            button_url=reset_url,
            button_text="Reset password",
        )
        logger.info("Queued password reset email", extra={"user_id": user.id, "task_id": async_result.id})
    except Exception:
        logger.exception("Failed to queue password reset email", extra={"user_id": user.id})


async def _queue_invite_email(user: User, password: str, org_name: str | None) -> None:
    """Email an invited team member their login credentials. Never raises."""
    try:
        login_url = f"{settings.frontend_url}/login"
        async_result = send_templated_email_task.delay(
            user.email,
            f"You've been invited to join {org_name or 'DevX'}",
            "generic.html",
            org_name=org_name,
            heading=f"You've been invited to join {org_name or 'DevX'}",
            body_paragraphs=[
                "An account has been created for you on DevX. Use the credentials below to sign in.",
                "We recommend changing your password after your first login.",
            ],
            details=[
                {"label": "Email", "value": user.email},
                {"label": "Temporary password", "value": password},
            ],
            button_url=login_url,
            button_text="Log in to DevX",
        )
        logger.info("Queued invite email", extra={"user_id": user.id, "task_id": async_result.id})
    except Exception:
        logger.exception("Failed to queue invite email", extra={"user_id": user.id})


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

    # 7. Invalidate admin caches — new tenant + user change aggregate counts
    await cache_del("admin:overview")
    await cache_del_pattern("admin:tenants:*")

    # 8. Queue verification email in the background — never blocks registration
    await _queue_verification_email(user)

    # 9. Generate tokens and set cookies
    access_token = create_access_token({"sub": user.id, "tenant_id": tenant.id})
    refresh_token = create_refresh_token({"sub": user.id, "tenant_id": tenant.id})

    csrf_token = set_auth_cookies(response, access_token, refresh_token)

    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": tenant.id, "csrf_token": csrf_token, "access_token": access_token, "refresh_token": refresh_token})



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

    csrf_token = set_auth_cookies(response, access_token, refresh_token)

    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": user.tenant_id, "csrf_token": csrf_token, "access_token": access_token, "refresh_token": refresh_token})

class RefreshRequest(BaseModel):
    refresh_token: str | None = None

@router.post("/refresh", response_model=AuthSuccessResponse)
async def refresh_token(request: Request, response: Response, body: RefreshRequest | None = None, db: AsyncSession = Depends(get_db)):
    refresh_tok = request.cookies.get("refresh_token")
    if not refresh_tok and body and body.refresh_token:
        refresh_tok = body.refresh_token

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

    csrf_token = set_auth_cookies(response, access_token, new_refresh_token)

    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": user.tenant_id, "csrf_token": csrf_token, "access_token": access_token, "refresh_token": new_refresh_token})


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


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, db: AsyncSession = Depends(get_db)):
    """Redeem a one-time email verification token sent to the user's inbox."""
    user_id = await consume_email_verification_token(body.token)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Account not found")

    user.is_verified = True
    await db.commit()
    return {"success": True, "message": "Email verified"}


@router.post("/resend-verification")
async def resend_verification(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Resend the verification email to the currently authenticated user."""
    if current_user.is_verified:
        return {"success": True, "message": "Email already verified"}

    ip_addr = request.client.host if request.client else "unknown"
    allowed = await check_rate_limit(f"resend-verification:{current_user.id}:{ip_addr}", "auth.resend_verification", max_requests=3, window_seconds=3600)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    await _queue_verification_email(current_user)
    return {"success": True, "message": "Verification email sent"}


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Queue a password reset email if the account exists. Always returns success to avoid email enumeration."""
    ip_addr = request.client.host if request.client else "unknown"
    allowed = await check_rate_limit(f"forgot-password:{body.email}:{ip_addr}", "auth.forgot_password", max_requests=3, window_seconds=3600)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if user and user.password_hash:
        await _queue_password_reset_email(user)

    return {"success": True, "message": "If an account with that email exists, a reset link has been sent"}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Redeem a one-time password reset token and set a new password."""
    user_id = await consume_password_reset_token(body.token)
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Account not found")

    user.password_hash = hash_password(body.new_password)
    user.failed_attempts = 0
    user.locked_until = None
    await db.commit()
    return {"success": True, "message": "Password reset successful"}


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
            "full_name": current_user.full_name,
            "email": current_user.email,
            "role": current_user.role,
            "tenant_id": current_user.tenant_id,
            "tenant_name": tenant.name if tenant else None,
            "tenant_slug": tenant.slug if tenant else None,
            "avatar_url": current_user.avatar_url,
            "oauth_provider": current_user.oauth_provider,
            "is_verified": current_user.is_verified,
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
    if "full_name" in body:
        current_user.full_name = body["full_name"].strip() or None

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


@router.get("/team-members")
async def list_team_members(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("super_admin")),
):
    """Return all users in the current user's tenant (admin-only)."""
    cache_key = f"auth:team-members:{current_user.tenant_id}"
    cached = await cache_get(cache_key)
    if cached:
        return {"success": True, "data": cached}

    result = await db.execute(
        select(User).where(User.tenant_id == current_user.tenant_id).order_by(User.created_at)
    )
    users = result.scalars().all()
    data = [
        {
            "user_id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "role": u.role,
            "oauth_provider": u.oauth_provider,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]
    await cache_set(cache_key, data, TEAM_MEMBERS_TTL)
    return {"success": True, "data": data}


@router.delete("/team-members/{user_id}", status_code=204)
async def remove_team_member(
    user_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("super_admin")),
):
    """Remove a member from the tenant. Admins cannot remove themselves or other admins."""
    if user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot remove your own account")

    result = await db.execute(
        select(User).where(User.id == user_id, User.tenant_id == current_user.tenant_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=404, detail="Account not found in your organization")

    if target.role in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Admin accounts cannot be removed")

    ip_addr = request.client.host if request.client else None
    audit = AuditLog(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="user.remove",
        resource_type="user",
        resource_id=target.id,
        diff={"email": target.email, "role": target.role},
        ip=ip_addr,
    )
    db.add(audit)
    await db.delete(target)
    await db.commit()
    await cache_del(
        f"auth:team-members:{current_user.tenant_id}",
        "admin:overview",
    )


@router.post("/invite-member", response_model=InviteMemberResponse)
async def invite_member(
    body: InviteMemberRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role("super_admin")),
    _verified: User = Depends(require_verified_email),
):
    """Admin creates a team member account with a generated password."""
    # Verify email uniqueness — User.email is unique platform-wide (login has no org
    # selector), so an email can belong to exactly one tenant. Distinguish the two
    # conflict cases so the error isn't confusing when the email simply isn't in
    # *this* tenant's roster.
    result = await db.execute(select(User).where(User.email == body.email))
    existing = result.scalar_one_or_none()
    if existing:
        if existing.tenant_id == current_user.tenant_id:
            raise ConflictException("This person is already a member of your organization")
        raise ConflictException("This email is already registered to a different organization on DevX")

    password = _generate_password()
    new_user = User(
        tenant_id=current_user.tenant_id,
        email=str(body.email),
        full_name=body.full_name,
        password_hash=hash_password(password),
        role="member",
    )
    db.add(new_user)
    await db.flush()

    ip_addr = request.client.host if request.client else None
    audit = AuditLog(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="user.invite",
        resource_type="user",
        resource_id=new_user.id,
        diff={"email": new_user.email, "full_name": new_user.full_name, "role": new_user.role},
        ip=ip_addr,
    )
    db.add(audit)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictException("A user with this email already exists")

    await cache_del(
        f"auth:team-members:{current_user.tenant_id}",
        "admin:overview",
    )

    tenant_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
    tenant = tenant_result.scalar_one_or_none()
    await _queue_invite_email(new_user, password, tenant.name if tenant else None)

    return InviteMemberResponse(data={
        "user_id": new_user.id,
        "full_name": new_user.full_name,
        "email": new_user.email,
        "password": password,
        "role": new_user.role,
    })


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
            is_verified=True,  # GitHub already verified this identity
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    access_token = create_access_token({"sub": user.id, "tenant_id": user.tenant_id})
    refresh_token = create_refresh_token({"sub": user.id, "tenant_id": user.tenant_id})

    csrf_token = set_auth_cookies(response, access_token, refresh_token)

    return AuthSuccessResponse(data={"user_id": user.id, "role": user.role, "tenant_id": user.tenant_id, "csrf_token": csrf_token, "access_token": access_token, "refresh_token": refresh_token})
