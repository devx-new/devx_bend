import hashlib
import secrets
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

_KEY_PREFIX = "pk_live_"


class WidgetKey(Base):
    __tablename__ = "widget_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    allowed_origins: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @staticmethod
    def generate() -> tuple[str, str, str]:
        """Return (full_key, display_prefix, key_hash). Call once; store only prefix + hash."""
        raw = secrets.token_hex(32)
        full_key = f"{_KEY_PREFIX}{raw}"
        prefix = full_key[:16]  # "pk_live_" + first 8 hex chars
        key_hash = hashlib.sha256(full_key.encode()).hexdigest()
        return full_key, prefix, key_hash

    @staticmethod
    def hash_key(full_key: str) -> str:
        return hashlib.sha256(full_key.encode()).hexdigest()
