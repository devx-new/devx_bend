import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class NotificationsLog(Base):
    __tablename__ = "notifications_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), nullable=False)
    feedback_item_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("feedback_items.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="sent")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
