import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from jose import ExpiredSignatureError, JWTError, jwt
from jose.exceptions import JWTClaimsError
from passlib.context import CryptContext

from app.config import settings

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], bcrypt__rounds=settings.bcrypt_cost)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.access_token_expire_minutes))
    to_encode.update({"exp": expire, "type": "access"})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    import uuid as _uuid
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=settings.refresh_token_expire_days))
    # jti enables server-side revocation via Redis blocklist
    to_encode.update({"exp": expire, "type": "refresh", "jti": str(_uuid.uuid4())})
    return jwt.encode(to_encode, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except ExpiredSignatureError:
        logger.debug("JWT decode failed: token expired")
        return {}
    except JWTClaimsError as exc:
        logger.debug("JWT decode failed: invalid claims", error=str(exc))
        return {}
    except JWTError as exc:
        logger.debug("JWT decode failed: malformed token", error=str(exc))
        return {}


def generate_api_key() -> tuple[str, str]:
    raw = str(uuid.uuid4())
    hashed = pwd_context.hash(raw)
    return raw, hashed


def verify_api_key(raw: str, hashed: str) -> bool:
    return pwd_context.verify(raw, hashed)


def get_github_oauth_url() -> str:
    return (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={settings.github_client_id}"
        f"&redirect_uri={settings.github_redirect_uri}"
        f"&scope=user:email"
    )


async def exchange_github_code(code: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        return resp.json()
