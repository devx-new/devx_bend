from pydantic import BaseModel


class RoutingRuleCreate(BaseModel):
    condition: dict
    action_type: str
    action_config: dict


class RoutingRuleResponse(BaseModel):
    id: str
    tenant_id: str
    condition: dict
    action_type: str
    action_config: dict

    model_config = {"from_attributes": True}
