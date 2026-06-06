from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class FeedbackCreate(BaseModel):
    source: str = Field(..., max_length=50)
    external_id: str = Field(..., max_length=255)
    title: str = Field(..., max_length=500)
    body: str | None = None
    author_handle: str | None = Field(None, max_length=255)


class FeedbackUpdate(BaseModel):
    status: Literal["open", "in_progress", "resolved", "wont_fix"] | None = None


class FeedbackResponse(BaseModel):
    id: str
    tenant_id: str
    source: str
    external_id: str
    title: str
    body: str | None
    author_handle: str | None
    status: str
    category: str | None
    sentiment_score: float | None
    priority_score: float | None
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}
