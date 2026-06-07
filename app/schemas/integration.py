from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IntegrationCreate(BaseModel):
    provider: str = Field(..., max_length=50)
    credentials: dict | None = None


class IntegrationResponse(BaseModel):
    id: str
    tenant_id: str
    provider: str
    status: str
    last_sync_at: datetime | None
    config: dict | None = None

    model_config = {"from_attributes": True}


class ProviderCatalogItem(BaseModel):
    provider: str
    name: str
    description: str
    scopes: list[str]
    coming_soon: bool
    status: str                    # "connected" | "not_connected"
    last_sync_at: datetime | None
    meta: dict | None              # active_repos, mapped_teams, active_channels, etc.
    integration_id: str | None


class IntegrationConfigureRequest(BaseModel):
    """Payload for step 2+3 of the Connect wizard."""
    # Provider-specific selections:
    # GitHub:  repos  = ["owner/repo", ...]
    # Linear:  teams  = [{"id": "...", "name": "..."}]
    # Slack:   channels = [{"id": "...", "name": "..."}]
    selections: dict[str, Any] = Field(default_factory=dict)
    routing_rules: list[dict] = Field(default_factory=list)
