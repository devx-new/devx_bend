import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.cookies import ACCESS_TOKEN_COOKIE
from app.core.rate_limit import get_redis
from app.database import get_db
from app.models.feedback import FeedbackItem
from app.models.survey import DevexSurvey
from app.models.digest import WeeklyDigest
from app.models.user import User
from app.schemas.analytics import AnalyticsSummaryResponse
from app.schemas.survey import SurveyCreate, SurveyResponse
from app.security import decode_token

router = APIRouter(prefix="/analytics", tags=["analytics"])

ANALYTICS_CACHE_TTL = 300  # 5 minutes


@router.get("/kpis")
async def analytics_kpis(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Key performance indicators - cached in Redis for 5 minutes."""
    tenant_id = current_user.tenant_id
    cache_key = f"analytics:kpis:{tenant_id}"

    try:
        r = await get_redis()
        cached = await r.get(cache_key)
        if cached:
            return {"success": True, "data": json.loads(cached), "meta": {"cached": True}}
    except Exception:
        pass  # Redis unavailable — fall through to DB

    total = await db.scalar(
        select(func.count()).select_from(FeedbackItem).where(FeedbackItem.tenant_id == tenant_id)
    )
    open_count = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.status == "open")
    )
    critical_count = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.priority_score > 80)
    )
    avg_sentiment = await db.scalar(
        select(func.avg(FeedbackItem.sentiment_score))
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.sentiment_score.isnot(None))
    )

    cat_result = await db.execute(
        select(FeedbackItem.category, func.count().label("count"))
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.category.isnot(None))
        .group_by(FeedbackItem.category)
        .order_by(func.count().desc())
    )
    categories = {row.category: row.count for row in cat_result}

    data = {
        "total_items": total or 0,
        "open_count": open_count or 0,
        "critical_count": critical_count or 0,
        "avg_sentiment": round(float(avg_sentiment), 3) if avg_sentiment else None,
        "top_categories": categories,
    }

    try:
        r = await get_redis()
        await r.setex(cache_key, ANALYTICS_CACHE_TTL, json.dumps(data))
    except Exception:
        pass

    return {"success": True, "data": data, "meta": {"cached": False}}


@router.get("/summary")
async def analytics_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    total = await db.scalar(
        select(func.count()).select_from(FeedbackItem).where(FeedbackItem.tenant_id == tenant_id)
    )
    open_count = await db.scalar(
        select(func.count())
        .select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.status == "open")
    )

    result = await db.execute(
        select(
            FeedbackItem.category,
            func.count().label("count"),
        )
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.category.isnot(None))
        .group_by(FeedbackItem.category)
        .order_by(func.count().desc())
    )
    categories = {row.category: row.count for row in result}

    return {
        "success": True,
        "message": {
            "open_count": open_count or 0,
            "total_items": total or 0,
            "top_categories": categories,
            "avg_resolution_time": None,
        },
    }


@router.get("/trends")
async def analytics_trends(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    days: int = Query(30, ge=1, le=365),
):
    tenant_id = current_user.tenant_id
    cache_key = f"analytics:trends:{tenant_id}:{days}"

    try:
        r = await get_redis()
        cached = await r.get(cache_key)
        if cached:
            return {"success": True, "data": json.loads(cached), "meta": {"cached": True}}
    except Exception:
        pass

    since = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(
            func.date(FeedbackItem.created_at).label("date"),
            func.count().label("count"),
            func.avg(FeedbackItem.priority_score).label("avg_priority"),
            func.avg(FeedbackItem.sentiment_score).label("avg_sentiment"),
            FeedbackItem.category
        )
        .where(
            FeedbackItem.tenant_id == tenant_id,
            FeedbackItem.created_at >= since,
        )
        .group_by(FeedbackItem.category, func.date(FeedbackItem.created_at))
        .order_by(func.date(FeedbackItem.created_at))
    )
    daily = [
        {
            "date": str(row.date),
            "count": row.count,
            "category": row.category or "uncategorized",
            "avg_priority": round(float(row.avg_priority), 1) if row.avg_priority else None,
            "avg_sentiment": round(float(row.avg_sentiment), 3) if row.avg_sentiment else None,
        }
        for row in result
    ]

    data = {"daily": daily, "period_days": days}
    try:
        r = await get_redis()
        await r.setex(cache_key, ANALYTICS_CACHE_TTL, json.dumps(data))
    except Exception:
        pass

    return {"success": True, "data": data, "meta": {"cached": False}}


@router.get("/devex-scores")
async def devex_scores(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    sql = text("""
        SELECT COALESCE(avg(score), 0) as overall_devex_score, count(score) as total_response, COALESCE(sum(
            case when score >= 9 then score else 0 end
            -
            case when score <=6 and score >=0 then score else 0 end 
        ), 0) as nps_score FROM devex_surveys WHERE tenant_id=:tenant_id limit 1 
    """)

    result = await db.execute(sql, {"tenant_id": tenant_id})
    total_responses = 0
    overall_devex_score = 0
    nps_score = 0
    category_scores = []
    for row in result:
        overall_devex_score = row.overall_devex_score
        total_responses = total_responses +  row.total_response
        nps_score = row.nps_score
    result = await db.execute(
        select(
            func.avg(DevexSurvey.score).label("score"), DevexSurvey.survey_type
        ).where(DevexSurvey.tenant_id == tenant_id).group_by(DevexSurvey.survey_type)
    )
    category_scores = result.scalars().all()
    return {
        "success": True,
        "message": {
            "overall_devex_score": overall_devex_score,
            "nps_score": nps_score,
            "total_responses": total_responses,
            "category_scores": category_scores
    },
    }


@router.post("/surveys", response_model=SurveyResponse)
async def submit_survey(
    body: SurveyCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    survey = DevexSurvey(
        tenant_id=tenant_id,
        user_id=None if body.anonymous else current_user.id,
        score=body.score,
        comment=body.comment,
        survey_type=body.survey_type,
    )
    db.add(survey)
    await db.commit()
    await db.refresh(survey)
    return survey


# Browsers automatically include cookies on same-origin WebSocket upgrade requests.
# No token query param needed — the access_token HttpOnly cookie is sent automatically.
# CSRF validation is skipped for WebSocket (browsers cannot set custom headers on WS upgrades).


@router.get("/digests")
async def list_digests(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    page: int = 1,
    per_page: int = 10,
):
    """Paginated list of weekly digest reports for the tenant."""
    from sqlalchemy import desc as sa_desc
    tenant_id = current_user.tenant_id
    total = await db.scalar(
        select(func.count()).select_from(WeeklyDigest).where(WeeklyDigest.tenant_id == tenant_id)
    )
    result = await db.execute(
        select(WeeklyDigest)
        .where(WeeklyDigest.tenant_id == tenant_id)
        .order_by(sa_desc(WeeklyDigest.created_at))
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    digests = result.scalars().all()
    return {
        "success": True,
        "data": [
            {
                "id": d.id,
                "report_markdown": d.report_markdown,
                "period_start": d.period_start.isoformat(),
                "period_end": d.period_end.isoformat(),
                "created_at": d.created_at.isoformat(),
            }
            for d in digests
        ],
        "meta": {"page": page, "per_page": per_page, "total": total or 0},
    }


@router.websocket("/ws/feed")
async def ws_feed(websocket: WebSocket):
    token = websocket.cookies.get(ACCESS_TOKEN_COOKIE)
    if not token:
        await websocket.close(code=4001, reason="Not authenticated")
        return

    payload = decode_token(token)
    if not payload.get("sub") or payload.get("type") != "access":
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
            await websocket.send_json({"status": "stub"})
    except WebSocketDisconnect:
        pass
