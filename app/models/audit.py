import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy import JSON  # FIX #21 — use generic JSON instead of PostgreSQL-specific JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    # FIX #17 — single UUID primary key; ts is NOT a pk column (composite PK broke
    #   append-only guarantee: two rows with same id but different ts were possible).
    #   Added index=True on ts for efficient time-range queries.
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
