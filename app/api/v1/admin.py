import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.config import settings
from app.core.cache import (
    ADMIN_OVERVIEW_TTL,
    ADMIN_TENANTS_TTL,
    cache_del,
    cache_del_pattern,
    cache_get,
    cache_set,
)
from app.database import get_db
from app.models.audit import AuditLog
from app.models.digest import WeeklyDigest
from app.models.feedback import DuplicateGroup, FeedbackItem, FeedbackTag
from app.models.integration import Integration
from app.models.notification import NotificationsLog
from app.models.routing import RoutingRule
from app.models.survey import DevexSurvey
from app.models.tenant import Tenant
from app.models.user import User
from app.models.widget_key import WidgetKey

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Promote a user to super_admin by email + secret key
# ---------------------------------------------------------------------------

class PromoteRequest(BaseModel):
    email: str
    secret: str


@router.post("/promote")
async def promote_super_admin(
    body: PromoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Promote a user to super_admin. Protected by SUPER_ADMIN_SECRET env var — no auth cookie required."""
    if not settings.super_admin_secret:
        raise HTTPException(status_code=503, detail="Super admin promotion is not configured")

    if not hmac.compare_digest(body.secret, settings.super_admin_secret):
        raise HTTPException(status_code=403, detail="Invalid secret")

    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = "super_admin"
    await db.commit()

    return {
        "success": True,
        "data": {
            "user_id": user.id,
            "email": user.email,
            "role": user.role,
        },
    }


# ---------------------------------------------------------------------------
# Platform Overview — KPIs across all tenants
# ---------------------------------------------------------------------------

@router.get("/overview")
async def admin_overview(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    cached = await cache_get("admin:overview")
    if cached:
        return {"success": True, "data": cached}

    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    prev_week_start = now - timedelta(days=14)

    total_tenants = await db.scalar(select(func.count()).select_from(Tenant))
    total_users = await db.scalar(select(func.count()).select_from(User))
    total_feedback = await db.scalar(select(func.count()).select_from(FeedbackItem))
    active_integrations = await db.scalar(
        select(func.count()).select_from(Integration)
        .where(Integration.status == "active")
    )

    feedback_this_week = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.created_at >= week_ago)
    )
    feedback_prev_week = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.created_at >= prev_week_start, FeedbackItem.created_at < week_ago)
    )

    # Plan tier breakdown
    plan_result = await db.execute(
        select(Tenant.plan_tier, func.count().label("count"))
        .group_by(Tenant.plan_tier)
    )
    plan_breakdown = {row.plan_tier: row.count for row in plan_result}

    week_delta = None
    if feedback_prev_week:
        week_delta = round(
            (((feedback_this_week or 0) - feedback_prev_week) / feedback_prev_week) * 100, 1
        )

    data = {
        "total_tenants": total_tenants or 0,
        "total_users": total_users or 0,
        "total_feedback": total_feedback or 0,
        "active_integrations": active_integrations or 0,
        "feedback_this_week": feedback_this_week or 0,
        "feedback_prev_week": feedback_prev_week or 0,
        "feedback_week_delta_pct": week_delta,
        "plan_breakdown": plan_breakdown,
    }
    await cache_set("admin:overview", data, ADMIN_OVERVIEW_TTL)
    return {"success": True, "data": data}


# ---------------------------------------------------------------------------
# All Tenants — list with per-tenant stats
# ---------------------------------------------------------------------------

@router.get("/tenants")
async def admin_tenants(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    cache_key = f"admin:tenants:{limit}:{offset}"
    cached = await cache_get(cache_key)
    if cached:
        return cached

    total = await db.scalar(select(func.count()).select_from(Tenant))

    tenants_result = await db.execute(
        select(Tenant).order_by(desc(Tenant.created_at)).offset(offset).limit(limit)
    )
    tenants = tenants_result.scalars().all()
    tenant_ids = [t.id for t in tenants]

    # User counts per tenant
    user_counts_result = await db.execute(
        select(User.tenant_id, func.count().label("count"))
        .where(User.tenant_id.in_(tenant_ids))
        .group_by(User.tenant_id)
    )
    user_counts = {row.tenant_id: row.count for row in user_counts_result}

    # Primary email per tenant (earliest-created user)
    owner_email_result = await db.execute(
        select(User.tenant_id, func.min(User.email).label("email"))
        .where(User.tenant_id.in_(tenant_ids))
        .group_by(User.tenant_id)
    )
    owner_emails = {row.tenant_id: row.email for row in owner_email_result}

    # Feedback counts per tenant
    feedback_counts_result = await db.execute(
        select(FeedbackItem.tenant_id, func.count().label("count"))
        .where(FeedbackItem.tenant_id.in_(tenant_ids))
        .group_by(FeedbackItem.tenant_id)
    )
    feedback_counts = {row.tenant_id: row.count for row in feedback_counts_result}

    # Latest feedback date per tenant
    last_feedback_result = await db.execute(
        select(FeedbackItem.tenant_id, func.max(FeedbackItem.created_at).label("last_at"))
        .where(FeedbackItem.tenant_id.in_(tenant_ids))
        .group_by(FeedbackItem.tenant_id)
    )
    last_feedback = {row.tenant_id: row.last_at for row in last_feedback_result}

    # Integration counts per tenant
    integration_counts_result = await db.execute(
        select(Integration.tenant_id, func.count().label("count"))
        .where(Integration.tenant_id.in_(tenant_ids), Integration.status == "active")
        .group_by(Integration.tenant_id)
    )
    integration_counts = {row.tenant_id: row.count for row in integration_counts_result}

    rows = [
        {
            "id": t.id,
            "name": t.name,
            "email": owner_emails.get(t.id),
            "slug": t.slug,
            "plan_tier": t.plan_tier,
            "user_count": user_counts.get(t.id, 0),
            "feedback_count": feedback_counts.get(t.id, 0),
            "integration_count": integration_counts.get(t.id, 0),
            "last_feedback_at": last_feedback[t.id].isoformat() if last_feedback.get(t.id) else None,
            "joined_at": t.created_at.isoformat(),
        }
        for t in tenants
    ]

    result = {
        "success": True,
        "data": rows,
        "meta": {"total": total or 0, "limit": limit, "offset": offset},
    }
    await cache_set(cache_key, result, ADMIN_TENANTS_TTL)
    return result


# ---------------------------------------------------------------------------
# Platform-wide feedback trends
# ---------------------------------------------------------------------------

@router.get("/trends")
async def admin_trends(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)

    daily_result = await db.execute(
        select(
            func.date(FeedbackItem.created_at).label("date"),
            func.count().label("count"),
        )
        .where(FeedbackItem.created_at >= since)
        .group_by(func.date(FeedbackItem.created_at))
        .order_by(func.date(FeedbackItem.created_at))
    )
    daily = [{"date": str(row.date), "count": row.count} for row in daily_result]

    return {"success": True, "data": {"daily": daily, "period_days": days}}


# ---------------------------------------------------------------------------
# Audit Logs — platform-wide, super_admin only
# ---------------------------------------------------------------------------

@router.get("/audit-logs")
async def audit_logs(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    action: str | None = Query(None),
    tenant_id: str | None = Query(None),
    search: str | None = Query(None, description="Free-text search across action, resource type, IP, actor email/name, tenant name"),
    since: str | None = Query(None, description="ISO timestamp — return only entries after this point"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_super_admin),
):
    query = select(AuditLog).order_by(desc(AuditLog.ts))

    if action:
        query = query.where(AuditLog.action == action)
    if tenant_id:
        query = query.where(AuditLog.tenant_id == tenant_id)
    if search:
        term = f"%{search}%"
        matching_actor_ids = select(User.id).where(
            or_(User.email.ilike(term), User.full_name.ilike(term))
        )
        matching_tenant_ids = select(Tenant.id).where(Tenant.name.ilike(term))
        query = query.where(
            or_(
                AuditLog.action.ilike(term),
                AuditLog.resource_type.ilike(term),
                AuditLog.ip.ilike(term),
                AuditLog.actor_id.in_(matching_actor_ids),
                AuditLog.tenant_id.in_(matching_tenant_ids),
            )
        )
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.where(AuditLog.ts > since_dt)
        except ValueError:
            pass

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows_result = await db.execute(query.offset((page - 1) * per_page).limit(per_page))
    rows = rows_result.scalars().all()

    # Resolve actor emails and tenant names in bulk
    actor_ids = list({r.actor_id for r in rows if r.actor_id})
    tenant_ids = list({r.tenant_id for r in rows})

    actors: dict[str, str] = {}
    if actor_ids:
        users_result = await db.execute(select(User).where(User.id.in_(actor_ids)))
        for u in users_result.scalars().all():
            actors[u.id] = u.full_name or u.email

    tenants: dict[str, str] = {}
    if tenant_ids:
        tenants_result = await db.execute(select(Tenant).where(Tenant.id.in_(tenant_ids)))
        for t in tenants_result.scalars().all():
            tenants[t.id] = t.name

    return {
        "success": True,
        "data": [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "action": r.action,
                "actor_id": r.actor_id,
                "actor_label": actors.get(r.actor_id, "system") if r.actor_id else "system",
                "tenant_id": r.tenant_id,
                "tenant_name": tenants.get(r.tenant_id, r.tenant_id),
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "diff": r.diff,
                "ip": r.ip,
            }
            for r in rows
        ],
        "meta": {"total": total or 0, "page": page, "per_page": per_page},
    }


# ---------------------------------------------------------------------------
# Delete a tenant (account) and all of its data — super_admin only
# ---------------------------------------------------------------------------

@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_super_admin),
):
    """
    Permanently delete a tenant and every row scoped to it. There are no DB-level
    cascades on tenant_id (see migrations), so child tables are wiped in FK-safe
    order before the tenant row itself. Irreversible.
    """
    if tenant_id == current_user.tenant_id:
        raise HTTPException(status_code=400, detail="You cannot delete your own organization")

    tenant = await db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_name, tenant_slug = tenant.name, tenant.slug
    feedback_ids_subq = select(FeedbackItem.id).where(FeedbackItem.tenant_id == tenant_id)

    await db.execute(delete(FeedbackTag).where(FeedbackTag.feedback_item_id.in_(feedback_ids_subq)))
    await db.execute(delete(DuplicateGroup).where(DuplicateGroup.tenant_id == tenant_id))
    await db.execute(delete(NotificationsLog).where(NotificationsLog.tenant_id == tenant_id))
    await db.execute(delete(FeedbackItem).where(FeedbackItem.tenant_id == tenant_id))
    await db.execute(delete(DevexSurvey).where(DevexSurvey.tenant_id == tenant_id))
    await db.execute(delete(WeeklyDigest).where(WeeklyDigest.tenant_id == tenant_id))
    await db.execute(delete(RoutingRule).where(RoutingRule.tenant_id == tenant_id))
    await db.execute(delete(Integration).where(Integration.tenant_id == tenant_id))
    await db.execute(delete(WidgetKey).where(WidgetKey.tenant_id == tenant_id))
    await db.execute(delete(AuditLog).where(AuditLog.tenant_id == tenant_id))
    await db.execute(delete(User).where(User.tenant_id == tenant_id))
    await db.delete(tenant)

    # Record the deletion under the acting super_admin's own tenant, since the
    # deleted tenant's audit trail was just wiped along with everything else.
    ip_addr = request.client.host if request.client else None
    db.add(AuditLog(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="tenant.delete",
        resource_type="tenant",
        resource_id=tenant_id,
        diff={"name": tenant_name, "slug": tenant_slug},
        ip=ip_addr,
    ))

    await db.commit()

    await cache_del("admin:overview")
    await cache_del_pattern("admin:tenants:*")
    await cache_del_pattern(f"auth:team-members:{tenant_id}")

    return {"success": True, "message": "Tenant deleted"}
