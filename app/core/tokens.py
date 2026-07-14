"""
One-time tokens for email verification and password reset.

Tokens are opaque random strings stored in Redis as `<prefix>:<token> -> user_id`
with a TTL, mirroring the revoked-jti pattern in app.api.v1.auth. Redeeming a
token deletes the key, so each token is single-use.
"""
import secrets

from app.core.rate_limit import get_redis

EMAIL_VERIFY_PREFIX = "email_verify"
PASSWORD_RESET_PREFIX = "pwd_reset"

EMAIL_VERIFY_TTL_SECONDS = 60 * 60 * 24  # 24h
PASSWORD_RESET_TTL_SECONDS = 60 * 60  # 1h


async def _create_token(prefix: str, user_id: str, ttl_seconds: int) -> str:
    token = secrets.token_urlsafe(32)
    r = await get_redis()
    await r.setex(f"{prefix}:{token}", ttl_seconds, user_id)
    return token


async def _consume_token(prefix: str, token: str) -> str | None:
    r = await get_redis()
    key = f"{prefix}:{token}"
    user_id = await r.get(key)
    if user_id is None:
        return None
    await r.delete(key)
    return user_id


async def create_email_verification_token(user_id: str) -> str:
    return await _create_token(EMAIL_VERIFY_PREFIX, user_id, EMAIL_VERIFY_TTL_SECONDS)


async def consume_email_verification_token(token: str) -> str | None:
    return await _consume_token(EMAIL_VERIFY_PREFIX, token)


async def create_password_reset_token(user_id: str) -> str:
    return await _create_token(PASSWORD_RESET_PREFIX, user_id, PASSWORD_RESET_TTL_SECONDS)


async def consume_password_reset_token(token: str) -> str | None:
    return await _consume_token(PASSWORD_RESET_PREFIX, token)
