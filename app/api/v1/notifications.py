"""
In-app notification inbox.

Uses Redis to store a per-user `last_read_at` timestamp.
Feed items created before or at that timestamp are "read".
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.rate_limit import get_redis
from app.database import get_db
from app.models.feedback import FeedbackItem
from app.models.user import User

router = APIRouter(prefix="/notifications", tags=["notifications"])
logger = logging.getLogger(__name__)

_REDIS_KEY = "notif:last_read:{user_id}"


def _utc(dt: datetime) -> datetime:
    """Normalise to timezone-aware UTC regardless of how the DB/driver returns the datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _get_last_read(user_id: str) -> datetime | None:
    try:
        r = await get_redis()
        raw = await r.get(_REDIS_KEY.format(user_id=user_id))
        if raw:
            return datetime.fromisoformat(raw.decode())
    except Exception:
        pass
    return None


@router.get("")
async def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return recent feedback items with an `is_read` flag based on the user's last-read timestamp."""
    last_read = await _get_last_read(current_user.id)

    result = await db.execute(
        select(FeedbackItem)
        .where(FeedbackItem.tenant_id == current_user.tenant_id)
        .order_by(desc(FeedbackItem.created_at))
        .limit(limit)
    )
    items = result.scalars().all()

    def serialize(item: FeedbackItem) -> dict:
        is_read = last_read is not None and _utc(item.created_at) <= last_read
        return {
            "id": item.id,
            "title": item.title,
            "category": item.category,
            "source": item.source,
            "status": item.status,
            "priority_score": item.priority_score,
            "created_at": item.created_at.isoformat(),
            "is_read": is_read,
        }

    return {"success": True, "data": [serialize(i) for i in items]}


@router.post("/read-all", status_code=200)
async def mark_all_read(
    current_user: User = Depends(get_current_user),
):
    """Persist last-read timestamp to Redis. Raises 503 if Redis is unavailable so the client can react."""
    now = datetime.now(timezone.utc)
    try:
        r = await get_redis()
        await r.set(_REDIS_KEY.format(user_id=current_user.id), now.isoformat())
    except Exception as exc:
        logger.error(
            "Failed to persist notifications read-all",
            extra={"user_id": current_user.id, "error": str(exc)},
        )
        raise HTTPException(status_code=503, detail="Could not persist read state — try again")

    logger.info(
        "Notifications marked as read",
        extra={"user_id": current_user.id, "tenant_id": current_user.tenant_id, "at": now.isoformat()},
    )
    return {"success": True}
