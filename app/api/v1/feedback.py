from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.cache import cache_del_pattern
from app.core.notifications import notify_feedback_resolved
from app.core.rate_limit import RateLimitDep
from app.database import get_db
from app.models.audit import AuditLog
from app.models.feedback import FeedbackItem
from app.models.user import User
from app.schemas.feedback import FeedbackCreate, FeedbackResponse, FeedbackUpdate

router = APIRouter(prefix="/feedback", tags=["feedback"])


async def write_audit_log(
    db: AsyncSession,
    tenant_id: str,
    actor_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    diff: dict | None = None,
    ip: str | None = None,
):
    entry = AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        diff=diff,
        ip=ip,
    )
    db.add(entry)
    await db.flush()


@router.post("", response_model=FeedbackResponse, dependencies=[Depends(RateLimitDep("feedback.create"))])
async def create_feedback(
    body: FeedbackCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    item = FeedbackItem(
        tenant_id=tenant_id,
        source=body.source,
        external_id=body.external_id,
        title=body.title,
        body=body.body,
        author_handle=body.author_handle or current_user.email,
    )
    db.add(item)
    await db.flush()
    await write_audit_log(db, tenant_id, current_user.id, "CREATE", "feedback_item", item.id)
    await db.commit()
    await db.refresh(item)
    await cache_del_pattern(f"analytics:{tenant_id}:*")

    from app.worker.orchestrator import start_feedback_pipeline
    start_feedback_pipeline.delay(item.id, tenant_id)

    return item


@router.get("")
async def list_feedback(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    category: str | None = None,
    status: str | None = None,
    source: str | None = None,
    search: str | None = None,
):
    tenant_id = current_user.tenant_id
    query = select(FeedbackItem).where(FeedbackItem.tenant_id == tenant_id)

    if category:
        query = query.where(FeedbackItem.category == category)
    if status:
        query = query.where(FeedbackItem.status == status)
    if source:
        query = query.where(FeedbackItem.source == source)
    if search:
        term = f"%{search}%"
        query = query.where(
            or_(FeedbackItem.title.ilike(term), FeedbackItem.body.ilike(term))
        )

    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()

    query = query.order_by(FeedbackItem.created_at.desc()).offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(query)
    items = result.scalars().all()

    return {
        "success": True,
        "message": [FeedbackResponse.model_validate(i).model_dump() for i in items],
        "meta": {"page": page, "per_page": per_page, "total": total},
    }


@router.get("/{feedback_id}", response_model=FeedbackResponse)
async def get_feedback(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    result = await db.execute(
        select(FeedbackItem).where(FeedbackItem.id == feedback_id, FeedbackItem.tenant_id == tenant_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return item


@router.patch("/{feedback_id}/status", response_model=FeedbackResponse)
async def update_feedback_status(
    feedback_id: str,
    body: FeedbackUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    result = await db.execute(
        select(FeedbackItem).where(FeedbackItem.id == feedback_id, FeedbackItem.tenant_id == tenant_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Feedback not found")

    old_status = item.status
    if body.status:
        item.status = body.status
    if body.status in ("resolved", "wont_fix"):
        item.resolved_at = datetime.now(timezone.utc)
    else:
        item.resolved_at = None

    await write_audit_log(
        db,
        tenant_id,
        current_user.id,
        "UPDATE_STATUS",
        "feedback_item",
        item.id,
        diff={"before": {"status": old_status}, "after": {"status": item.status}},
    )
    await db.commit()
    await db.refresh(item)
    await cache_del_pattern(f"analytics:{tenant_id}:*")

    if old_status not in ("resolved", "wont_fix") and item.status in ("resolved", "wont_fix"):
        await notify_feedback_resolved(item, db)

    return item
