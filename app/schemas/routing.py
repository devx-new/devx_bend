from typing import Optional
from pydantic import BaseModel


class RoutingRuleCreate(BaseModel):
    name: Optional[str] = None
    enabled: bool = True
    condition: dict
    action_type: str
    action_config: dict


class RoutingRuleUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    condition: Optional[dict] = None
    action_type: Optional[str] = None
    action_config: Optional[dict] = None


class RoutingRuleResponse(BaseModel):
    id: str
    tenant_id: str
    name: Optional[str] = None
    enabled: bool
    condition: dict
    action_type: str
    action_config: dict

    model_config = {"from_attributes": True}
