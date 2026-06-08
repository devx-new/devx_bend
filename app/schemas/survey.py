from datetime import datetime
from pydantic import BaseModel, Field


class SurveyCreate(BaseModel):
    score: int = Field(..., ge=1, le=10)
    comment: str | None = None
    survey_type: str | None = Field(None, max_length=50)
    anonymous: bool = False


class SurveyResponse(BaseModel):
    id: str
    tenant_id: str
    score: int
    comment: str | None
    survey_type: str | None
    submitted_at: datetime

    model_config = {"from_attributes": True}
