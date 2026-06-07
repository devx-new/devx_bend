import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.cookies import ACCESS_TOKEN_COOKIE
from app.core.rate_limit import get_redis
from app.database import async_session_factory, get_db
from app.models.feedback import FeedbackItem
from app.models.survey import DevexSurvey
from app.models.digest import WeeklyDigest
from app.models.user import User
from app.schemas.analytics import AnalyticsSummaryResponse
from app.schemas.survey import SurveyCreate, SurveyResponse
from app.security import decode_token

router = APIRouter(prefix="/analytics", tags=["analytics"])

ANALYTICS_CACHE_TTL = 300  # 5 minutes

# Sentiment thresholds — VADER compound score is −1.0 to +1.0
# Standard VADER thresholds: positive ≥ 0.05, negative ≤ −0.05.
# We use slightly wider bands to match "frustrated / neutral / positive" UI labels.
_SENTIMENT_FRUSTRATED = -0.1   # compound < -0.1  → frustrated
_SENTIMENT_POSITIVE = 0.1      # compound >= 0.1  → positive


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _serialize_feed_item(item: FeedbackItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "category": item.category,
        "source": item.source,
        "status": item.status,
        "priority_score": item.priority_score,
        "sentiment_score": item.sentiment_score,
        "author_handle": item.author_handle,
        "created_at": item.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

@router.get("/kpis")
async def analytics_kpis(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """KPIs for the current period with period-over-period deltas — cached 5 min."""
    tenant_id = current_user.tenant_id
    cache_key = f"analytics:kpis:{tenant_id}:{days}"

    try:
        r = await get_redis()
        cached = await r.get(cache_key)
        if cached:
            return {"success": True, "data": json.loads(cached), "meta": {"cached": True}}
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_prev = now - timedelta(days=days * 2)

    # --- current period ---
    total = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.created_at >= since)
    )
    open_count = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.status == "open",
               FeedbackItem.created_at >= since)
    )
    resolved_count = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.resolved_at.isnot(None),
               FeedbackItem.resolved_at >= since)
    )
    critical_count = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.priority_score > 80,
               FeedbackItem.created_at >= since)
    )

    # --- previous period ---
    total_prev = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.created_at >= since_prev, FeedbackItem.created_at < since)
    )
    open_prev = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.status == "open",
               FeedbackItem.created_at >= since_prev, FeedbackItem.created_at < since)
    )
    resolved_prev = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.resolved_at.isnot(None),
               FeedbackItem.resolved_at >= since_prev, FeedbackItem.resolved_at < since)
    )

    # --- avg resolution time ---
    _epoch_diff = func.extract("epoch", FeedbackItem.resolved_at - FeedbackItem.created_at)

    avg_res_s = await db.scalar(
        select(func.avg(_epoch_diff))
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.resolved_at.isnot(None), FeedbackItem.created_at >= since)
    )
    avg_res_s_prev = await db.scalar(
        select(func.avg(_epoch_diff))
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.resolved_at.isnot(None),
               FeedbackItem.created_at >= since_prev, FeedbackItem.created_at < since)
    )
    avg_res_hours = round(float(avg_res_s) / 3600, 1) if avg_res_s else None
    avg_res_hours_prev = round(float(avg_res_s_prev) / 3600, 1) if avg_res_s_prev else None
    avg_res_hours_delta = (
        round(avg_res_hours - avg_res_hours_prev, 1)
        if avg_res_hours is not None and avg_res_hours_prev is not None
        else None
    )

    # --- avg sentiment ---
    avg_sentiment = await db.scalar(
        select(func.avg(FeedbackItem.sentiment_score))
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.sentiment_score.isnot(None), FeedbackItem.created_at >= since)
    )

    # --- category breakdown ---
    cat_result = await db.execute(
        select(FeedbackItem.category, func.count().label("count"))
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.category.isnot(None),
               FeedbackItem.created_at >= since)
        .group_by(FeedbackItem.category)
        .order_by(func.count().desc())
    )
    categories = {row.category: row.count for row in cat_result}

    # --- dev satisfaction (survey avg normalised to /5) ---
    avg_survey = await db.scalar(
        select(func.avg(DevexSurvey.score)).where(DevexSurvey.tenant_id == tenant_id)
    )
    dev_satisfaction = round(float(avg_survey) / 2, 1) if avg_survey else None

    data = {
        "total_items": total or 0,
        "open_count": open_count or 0,
        "resolved_count": resolved_count or 0,
        "critical_count": critical_count or 0,
        "avg_sentiment": round(float(avg_sentiment), 3) if avg_sentiment else None,
        "avg_resolution_hours": avg_res_hours,
        "avg_resolution_days": round(avg_res_hours / 24, 1) if avg_res_hours else None,
        "dev_satisfaction": dev_satisfaction,
        "top_categories": categories,
        "deltas": {
            "total_items_pct": _pct_change(total or 0, total_prev or 0),
            "open_count_pct": _pct_change(open_count or 0, open_prev or 0),
            "resolved_count_pct": _pct_change(resolved_count or 0, resolved_prev or 0),
            "avg_resolution_hours_delta": avg_res_hours_delta,
            "avg_resolution_hours_pct": _pct_change(avg_res_hours, avg_res_hours_prev),
        },
    }

    try:
        r = await get_redis()
        await r.setex(cache_key, ANALYTICS_CACHE_TTL, json.dumps(data))
    except Exception:
        pass

    return {"success": True, "data": data, "meta": {"cached": False}}


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

@router.get("/summary")
async def analytics_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    total = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id)
    )
    open_count = await db.scalar(
        select(func.count()).select_from(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.status == "open")
    )
    avg_res_s = await db.scalar(
        select(func.avg(
            func.extract("epoch", FeedbackItem.resolved_at - FeedbackItem.created_at)
        ))
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.resolved_at.isnot(None))
    )
    avg_resolution_days = round(float(avg_res_s) / 86400, 1) if avg_res_s else None

    result = await db.execute(
        select(FeedbackItem.category, func.count().label("count"))
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
            "avg_resolution_time": avg_resolution_days,
        },
    }


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------

@router.get("/trends")
async def analytics_trends(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
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

    # Per-category daily breakdown
    cat_result = await db.execute(
        select(
            func.date(FeedbackItem.created_at).label("date"),
            func.count().label("count"),
            func.avg(FeedbackItem.priority_score).label("avg_priority"),
            func.avg(FeedbackItem.sentiment_score).label("avg_sentiment"),
            FeedbackItem.category,
        )
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.created_at >= since)
        .group_by(FeedbackItem.category, func.date(FeedbackItem.created_at))
        .order_by(func.date(FeedbackItem.created_at))
    )
    daily_by_category = [
        {
            "date": str(row.date),
            "count": row.count,
            "category": row.category or "uncategorized",
            "avg_priority": round(float(row.avg_priority), 1) if row.avg_priority else None,
            "avg_sentiment": round(float(row.avg_sentiment), 3) if row.avg_sentiment else None,
        }
        for row in cat_result
    ]

    # Total per day — Feedback Volume line chart
    total_result = await db.execute(
        select(
            func.date(FeedbackItem.created_at).label("date"),
            func.count().label("count"),
        )
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.created_at >= since)
        .group_by(func.date(FeedbackItem.created_at))
        .order_by(func.date(FeedbackItem.created_at))
    )
    total_daily = [{"date": str(row.date), "count": row.count} for row in total_result]

    # Total per week — Weekly toggle on Feedback Volume chart
    # Use text() literal to avoid PostgreSQL grouping error caused by
    # SQLAlchemy parameterising the 'week' string differently in SELECT vs GROUP BY.
    from sqlalchemy import literal_column
    week_expr = literal_column("date_trunc('week', created_at)")
    weekly_result = await db.execute(
        select(
            week_expr.label("week"),
            func.count().label("count"),
        )
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.created_at >= since)
        .group_by(week_expr)
        .order_by(week_expr)
        .select_from(FeedbackItem)
    )
    total_weekly = [
        {"week": row.week.isoformat(), "count": row.count}
        for row in weekly_result
    ]

    data = {
        "daily": daily_by_category,
        "total_daily": total_daily,
        "total_weekly": total_weekly,
        "period_days": days,
    }
    try:
        r = await get_redis()
        await r.setex(cache_key, ANALYTICS_CACHE_TTL, json.dumps(data))
    except Exception:
        pass

    return {"success": True, "data": data, "meta": {"cached": False}}


# ---------------------------------------------------------------------------
# Sentiment breakdown
# ---------------------------------------------------------------------------

@router.get("/sentiment")
async def sentiment_breakdown(
    days: int = Query(7, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Frustrated / Neutral / Positive counts with trend vs prior period."""
    tenant_id = current_user.tenant_id
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_prev = now - timedelta(days=days * 2)

    def _sentiment_counts(rows):
        frustrated = neutral = positive = 0
        for row in rows:
            if row.sentiment_score < _SENTIMENT_FRUSTRATED:
                frustrated += 1
            elif row.sentiment_score >= _SENTIMENT_POSITIVE:
                positive += 1
            else:
                neutral += 1
        return frustrated, neutral, positive

    curr_rows = (await db.execute(
        select(FeedbackItem.sentiment_score)
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.sentiment_score.isnot(None),
               FeedbackItem.created_at >= since)
    )).all()

    prev_rows = (await db.execute(
        select(FeedbackItem.sentiment_score)
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.sentiment_score.isnot(None),
               FeedbackItem.created_at >= since_prev,
               FeedbackItem.created_at < since)
    )).all()

    c_frust, c_neut, c_pos = _sentiment_counts(curr_rows)
    p_frust, p_neut, p_pos = _sentiment_counts(prev_rows)
    total_curr = c_frust + c_neut + c_pos or 1

    return {
        "success": True,
        "data": {
            "frustrated": {"count": c_frust, "pct": round(c_frust / total_curr * 100, 1)},
            "neutral": {"count": c_neut, "pct": round(c_neut / total_curr * 100, 1)},
            "positive": {"count": c_pos, "pct": round(c_pos / total_curr * 100, 1)},
            "deltas": {
                "frustrated_pct": _pct_change(c_frust, p_frust),
                "positive_pct": _pct_change(c_pos, p_pos),
            },
            "period_days": days,
        },
    }


# ---------------------------------------------------------------------------
# Spike time-series (mini charts on AI Insights)
# ---------------------------------------------------------------------------

@router.get("/spikes")
async def category_spikes(
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Top spiking categories with daily time-series for mini charts."""
    tenant_id = current_user.tenant_id
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_prev = now - timedelta(days=days * 2)

    # Category counts per window
    curr_result = await db.execute(
        select(FeedbackItem.category, func.count().label("count"))
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.created_at >= since, FeedbackItem.category.isnot(None))
        .group_by(FeedbackItem.category)
    )
    curr_counts = {row.category: row.count for row in curr_result}

    prev_result = await db.execute(
        select(FeedbackItem.category, func.count().label("count"))
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.created_at >= since_prev, FeedbackItem.created_at < since,
               FeedbackItem.category.isnot(None))
        .group_by(FeedbackItem.category)
    )
    prev_counts = {row.category: row.count for row in prev_result}

    # Rank categories by spike magnitude
    all_cats = set(curr_counts) | set(prev_counts)
    ranked = sorted(
        all_cats,
        key=lambda c: _pct_change(curr_counts.get(c, 0), prev_counts.get(c, 0)) or 0,
        reverse=True,
    )[:limit]

    # Daily time-series for each top-spiking category
    series_result = await db.execute(
        select(
            FeedbackItem.category,
            func.date(FeedbackItem.created_at).label("date"),
            func.count().label("count"),
        )
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.created_at >= since_prev,
               FeedbackItem.category.in_(ranked))
        .group_by(FeedbackItem.category, func.date(FeedbackItem.created_at))
        .order_by(FeedbackItem.category, func.date(FeedbackItem.created_at))
    )
    series_by_cat: dict[str, list] = {}
    for row in series_result:
        series_by_cat.setdefault(row.category, []).append(
            {"date": str(row.date), "count": row.count}
        )

    spikes = [
        {
            "category": cat,
            "current_count": curr_counts.get(cat, 0),
            "prev_count": prev_counts.get(cat, 0),
            "spike_pct": _pct_change(curr_counts.get(cat, 0), prev_counts.get(cat, 0)),
            "series": series_by_cat.get(cat, []),
        }
        for cat in ranked
    ]

    return {"success": True, "data": {"spikes": spikes, "period_days": days}}


# ---------------------------------------------------------------------------
# Resolution-time distribution
# ---------------------------------------------------------------------------

@router.get("/resolution-distribution")
async def resolution_distribution(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Histogram of resolution times bucketed by days-to-close."""
    tenant_id = current_user.tenant_id
    since = datetime.now(timezone.utc) - timedelta(days=days)

    days_to_close = (
        func.extract("epoch", FeedbackItem.resolved_at - FeedbackItem.created_at) / 86400
    )
    bucket = case(
        (days_to_close < 1, "0-1d"),
        (days_to_close < 3, "1-3d"),
        (days_to_close < 7, "3-7d"),
        (days_to_close < 14, "7-14d"),
        (days_to_close < 30, "14-30d"),
        else_="30d+",
    )

    result = await db.execute(
        select(bucket.label("bucket"), func.count().label("count"))
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.resolved_at.isnot(None),
               FeedbackItem.created_at >= since)
        .group_by(bucket)
    )
    counts = {row.bucket: row.count for row in result}
    BUCKETS = ["0-1d", "1-3d", "3-7d", "7-14d", "14-30d", "30d+"]
    distribution = [{"bucket": b, "count": counts.get(b, 0)} for b in BUCKETS]

    return {"success": True, "data": {"distribution": distribution, "period_days": days}}


# ---------------------------------------------------------------------------
# Trending topics
# ---------------------------------------------------------------------------

@router.get("/trending-topics")
async def trending_topics(
    days: int = Query(7, ge=1, le=30),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Top feedback items by priority score with category-level spike labels."""
    tenant_id = current_user.tenant_id
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_prev = now - timedelta(days=days * 2)

    curr_result = await db.execute(
        select(FeedbackItem.category, func.count().label("count"))
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.created_at >= since,
               FeedbackItem.category.isnot(None))
        .group_by(FeedbackItem.category)
    )
    curr_counts = {row.category: row.count for row in curr_result}

    prev_result = await db.execute(
        select(FeedbackItem.category, func.count().label("count"))
        .where(FeedbackItem.tenant_id == tenant_id,
               FeedbackItem.created_at >= since_prev, FeedbackItem.created_at < since,
               FeedbackItem.category.isnot(None))
        .group_by(FeedbackItem.category)
    )
    prev_counts = {row.category: row.count for row in prev_result}

    items_result = await db.execute(
        select(FeedbackItem.id, FeedbackItem.title, FeedbackItem.category,
               FeedbackItem.priority_score, FeedbackItem.source)
        .where(FeedbackItem.tenant_id == tenant_id, FeedbackItem.created_at >= since,
               FeedbackItem.priority_score.isnot(None))
        .order_by(desc(FeedbackItem.priority_score))
        .limit(limit)
    )

    topics = []
    for row in items_result:
        cat = row.category or "uncategorized"
        cur = curr_counts.get(cat, 0)
        prev = prev_counts.get(cat, 0)
        delta = cur - prev
        label = (
            f"Spike detected: +{delta} mentions" if delta > 0
            else f"Declining: {delta} mentions" if delta < 0
            else "Consistent top request"
        )
        topics.append({
            "id": row.id,
            "title": row.title,
            "category": cat,
            "priority_score": row.priority_score,
            "mention_count": cur,
            "spike_pct": _pct_change(cur, prev),
            "label": label,
        })

    return {"success": True, "data": {"topics": topics, "period_days": days}}


# ---------------------------------------------------------------------------
# DevEx scores
# ---------------------------------------------------------------------------

@router.get("/devex-scores")
async def devex_scores(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id

    agg = await db.execute(
        select(
            func.avg(DevexSurvey.score).label("avg_score"),
            func.count(DevexSurvey.score).label("total_responses"),
            (
                func.sum(case((DevexSurvey.score >= 9, 1), else_=0))
                - func.sum(case((DevexSurvey.score <= 6, 1), else_=0))
            ).label("nps_numerator"),
            func.nullif(func.count(DevexSurvey.score), 0).label("total_for_nps"),
        ).where(DevexSurvey.tenant_id == tenant_id)
    )
    row = agg.one()
    overall_devex_score = round(float(row.avg_score), 2) if row.avg_score else 0
    total_responses = row.total_responses or 0
    nps_score = (
        round(float(row.nps_numerator) / float(row.total_for_nps) * 100, 1)
        if row.nps_numerator is not None and row.total_for_nps
        else 0
    )

    cat_result = await db.execute(
        select(DevexSurvey.survey_type, func.avg(DevexSurvey.score).label("avg_score"))
        .where(DevexSurvey.tenant_id == tenant_id)
        .group_by(DevexSurvey.survey_type)
    )
    category_scores = [
        {"survey_type": r.survey_type, "avg_score": round(float(r.avg_score), 2)}
        for r in cat_result
    ]

    return {
        "success": True,
        "message": {
            "overall_devex_score": overall_devex_score,
            "nps_score": nps_score,
            "total_responses": total_responses,
            "category_scores": category_scores,
        },
    }


# ---------------------------------------------------------------------------
# Surveys
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------

@router.get("/digests")
async def list_digests(
    page: int = 1,
    per_page: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Paginated list of weekly digest reports for the tenant."""
    tenant_id = current_user.tenant_id
    total = await db.scalar(
        select(func.count()).select_from(WeeklyDigest).where(WeeklyDigest.tenant_id == tenant_id)
    )
    result = await db.execute(
        select(WeeklyDigest)
        .where(WeeklyDigest.tenant_id == tenant_id)
        .order_by(desc(WeeklyDigest.created_at))
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


@router.post("/digests/generate")
async def generate_digest(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Trigger an on-demand digest for the current tenant (runs in background)."""
    from app.agents.digest import _generate_digest_for_tenant_async

    async def _run():
        try:
            await _generate_digest_for_tenant_async(current_user.tenant_id)
        except Exception:
            pass  # errors are logged inside the function

    background_tasks.add_task(_run)
    return {"success": True, "message": "Digest generation started"}


# ---------------------------------------------------------------------------
# Real-time feed — REST polling endpoint
# ---------------------------------------------------------------------------

@router.get("/feed")
async def get_feed(
    limit: int = Query(20, ge=1, le=100),
    since: str | None = Query(None, description="ISO timestamp — return only items after this"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Latest feedback items for the real-time feed panel. Poll with ?since= for incremental updates."""
    tenant_id = current_user.tenant_id
    query = (
        select(FeedbackItem)
        .where(FeedbackItem.tenant_id == tenant_id)
        .order_by(desc(FeedbackItem.created_at))
        .limit(limit)
    )
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query = query.where(FeedbackItem.created_at > since_dt)
        except ValueError:
            pass

    result = await db.execute(query)
    items = result.scalars().all()
    return {
        "success": True,
        "data": [_serialize_feed_item(i) for i in items],
    }


# ---------------------------------------------------------------------------
# WebSocket real-time feed
# ---------------------------------------------------------------------------

# Browsers automatically include cookies on same-origin WebSocket upgrade requests.
# CSRF validation is skipped for WebSocket (browsers cannot set custom headers on WS upgrades).

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

    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        await websocket.close(code=4001, reason="Missing tenant context")
        return

    await websocket.accept()
    try:
        async with async_session_factory() as db:
            # Send initial batch (most recent 20 items, oldest first for display order)
            result = await db.execute(
                select(FeedbackItem)
                .where(FeedbackItem.tenant_id == tenant_id)
                .order_by(desc(FeedbackItem.created_at))
                .limit(20)
            )
            initial = list(reversed(result.scalars().all()))
            last_seen_at = initial[-1].created_at if initial else datetime.now(timezone.utc)
            await websocket.send_json({
                "type": "initial",
                "items": [_serialize_feed_item(i) for i in initial],
            })

            # Poll for new items every 5 seconds
            while True:
                await asyncio.sleep(5)
                new_result = await db.execute(
                    select(FeedbackItem)
                    .where(FeedbackItem.tenant_id == tenant_id,
                           FeedbackItem.created_at > last_seen_at)
                    .order_by(FeedbackItem.created_at)
                    .limit(20)
                )
                new_items = new_result.scalars().all()
                if new_items:
                    last_seen_at = new_items[-1].created_at
                    await websocket.send_json({
                        "type": "update",
                        "items": [_serialize_feed_item(i) for i in new_items],
                    })
    except WebSocketDisconnect:
        pass
