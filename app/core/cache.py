import json
from typing import Any

from app.core.rate_limit import get_redis

ANALYTICS_TTL = 300     # 5 min — per-tenant analytics aggregates
TEAM_MEMBERS_TTL = 60   # 1 min — team roster, changes infrequently
ADMIN_OVERVIEW_TTL = 30  # 30 s  — platform-wide counters
ADMIN_TENANTS_TTL = 120  # 2 min — tenant list pages


async def cache_get(key: str) -> Any | None:
    try:
        r = await get_redis()
        value = await r.get(key)
        return json.loads(value) if value else None
    except Exception:
        return None


async def cache_set(key: str, data: Any, ttl: int) -> None:
    try:
        r = await get_redis()
        await r.setex(key, ttl, json.dumps(data))
    except Exception:
        pass


async def cache_del(*keys: str) -> None:
    if not keys:
        return
    try:
        r = await get_redis()
        await r.delete(*keys)
    except Exception:
        pass


async def cache_del_pattern(pattern: str) -> None:
    """Delete all Redis keys matching a glob pattern via SCAN."""
    try:
        r = await get_redis()
        cursor = 0
        while True:
            cursor, keys = await r.scan(cursor, match=pattern, count=100)
            if keys:
                await r.delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass
