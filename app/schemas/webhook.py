from pydantic import BaseModel, Field


class WebhookPayload(BaseModel):
    provider: str = Field(..., max_length=50)
    raw: dict
