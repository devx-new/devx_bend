import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy import JSON  # FIX #21 — generic JSON works on both SQLite and PostgreSQL
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # NOTE: credentials contain third-party API tokens. In production these should be
    # encrypted at the application layer (e.g. Fernet) before storage — see Issue #13
    # in the security review. The encryption step is left for a dedicated secrets-management task.
    credentials: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    webhook_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="active")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_integrations_tenant_provider"),
    )
