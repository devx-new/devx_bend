"""
Rate limiting utilities.

Per-tenant sliding-window rate limiter backed by Redis sorted sets.
Usage as a FastAPI dependency:

    from app.core.rate_limit import RateLimitDep
    ...
    @router.post("")
    async def my_endpoint(..., _: None = Depends(RateLimitDep("feedback"))):
        ...
"""

import logging
import time

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)
_redis: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def check_rate_limit(
    tenant_id: str, endpoint: str, max_requests: int = 100, window_seconds: int = 60
) -> bool:
    """
    Sliding-window rate limiter using Redis sorted sets.
    Fails open (returns True) if Redis is unavailable — traffic is never
    blocked due to a Redis outage.
    """
    try:
        r = await get_redis()
        key = f"rate:{tenant_id}:{endpoint}"
        now = int(time.time())
        window_start = now - window_seconds

        await r.zremrangebyscore(key, "-inf", window_start)
        count = await r.zcard(key)

        if count >= max_requests:
            return False

        await r.zadd(key, {f"{now}:{time.monotonic_ns()}": now})
        await r.expire(key, window_seconds)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Rate limiter unavailable, failing open", error=str(exc))
        return True


def RateLimitDep(endpoint: str, max_requests: int = 100, window_seconds: int = 60):
    """
    FastAPI dependency factory for per-tenant rate limiting.

    Example:
        @router.post("", dependencies=[Depends(RateLimitDep("feedback.create"))])

    FIX #12 — exposes the rate limiter for use in route handlers/routers.
    """
    async def _check(
        request: Request,
        r: aioredis.Redis = Depends(get_redis),
    ) -> None:
        tenant_id = getattr(request.state, "tenant_id", None) or "anonymous"
        allowed = await check_rate_limit(tenant_id, endpoint, max_requests, window_seconds)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: max {max_requests} requests per {window_seconds}s",
            )

    return _check
