import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_super_admin
from app.config import settings
from app.database import get_db
from app.models.feedback import FeedbackItem
from app.models.integration import Integration
from app.models.tenant import Tenant
from app.models.user import User

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

    return {
        "success": True,
        "data": {
            "total_tenants": total_tenants or 0,
            "total_users": total_users or 0,
            "total_feedback": total_feedback or 0,
            "active_integrations": active_integrations or 0,
            "feedback_this_week": feedback_this_week or 0,
            "feedback_prev_week": feedback_prev_week or 0,
            "feedback_week_delta_pct": week_delta,
            "plan_breakdown": plan_breakdown,
        },
    }


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

    return {
        "success": True,
        "data": rows,
        "meta": {"total": total or 0, "limit": limit, "offset": offset},
    }


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
