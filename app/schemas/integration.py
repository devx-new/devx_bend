from pydantic import BaseModel, Field


class IntegrationCreate(BaseModel):
    provider: str = Field(..., max_length=50)
    credentials: dict | None = None


class IntegrationResponse(BaseModel):
    id: str
    tenant_id: str
    provider: str
    status: str
    last_sync_at: str | None

    model_config = {"from_attributes": True}
