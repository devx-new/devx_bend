from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.integration import Integration
from app.models.routing import RoutingRule
from app.models.user import User
from app.schemas.integration import IntegrationCreate, IntegrationResponse
from app.schemas.routing import RoutingRuleCreate, RoutingRuleResponse

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.post("", response_model=IntegrationResponse)
async def create_integration(
    body: IntegrationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    integration = Integration(
        tenant_id=tenant_id,
        provider=body.provider,
        credentials=body.credentials,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)
    return integration


@router.get("")
async def list_integrations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    result = await db.execute(select(Integration).where(Integration.tenant_id == tenant_id))
    items = result.scalars().all()
    return {
        "success": True,
        "message": [IntegrationResponse.model_validate(i).model_dump() for i in items],
    }


@router.post("/routing-rules", response_model=RoutingRuleResponse)
async def create_routing_rule(
    body: RoutingRuleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    rule = RoutingRule(
        tenant_id=tenant_id,
        condition=body.condition,
        action_type=body.action_type,
        action_config=body.action_config,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return rule


@router.get("/routing-rules")
async def list_routing_rules(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tenant_id = current_user.tenant_id
    result = await db.execute(select(RoutingRule).where(RoutingRule.tenant_id == tenant_id))
    items = result.scalars().all()
    return {
        "success": True,
        "message": [RoutingRuleResponse.model_validate(i).model_dump() for i in items],
    }
