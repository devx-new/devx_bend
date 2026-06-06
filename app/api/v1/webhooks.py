import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.feedback import FeedbackItem
from app.models.integration import Integration
from app.worker.orchestrator import start_feedback_pipeline

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

MAX_BODY_BYTES = 65_536  # 64 KB — matches AGENTS.md spec


async def _read_body(request: Request) -> bytes:
    """Read and size-check the raw request body."""
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large (max 64 KB)")
    return body


def _verify_hmac(payload: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)


async def _get_integration(request: Request, provider: str, db: AsyncSession) -> Integration:
    """Fetch the active integration for the given provider. Raises 400 if absent."""
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if not tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-ID header required")

    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == provider,
            Integration.status == "active",
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=400, detail=f"No active {provider} integration")

    # FIX #7 — reject early if no secret is configured; prevents HMAC bypass via empty key
    if not integration.webhook_secret:
        raise HTTPException(
            status_code=500,
            detail=f"{provider} integration webhook_secret not configured",
        )
    return integration


@router.post("/github")
async def github_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    integration = await _get_integration(request, "github", db)

    body = await _read_body(request)
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_hmac(body, signature, integration.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = json.loads(body)
    action = payload.get("action")
    issue = payload.get("issue", {})

    if action in ("opened", "created") and issue:
        item = FeedbackItem(
            tenant_id=integration.tenant_id,
            source="github",
            external_id=f"{payload.get('repository', {}).get('full_name', '')}#{issue.get('number')}",
            title=issue.get("title", ""),
            body=issue.get("body", ""),
            author_handle=issue.get("user", {}).get("login"),
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        start_feedback_pipeline.delay(item.id, integration.tenant_id)

    return {"success": True, "message": {"received": True}}


@router.post("/gitlab")
async def gitlab_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    integration = await _get_integration(request, "gitlab", db)

    body = await _read_body(request)
    token = request.headers.get("X-Gitlab-Token", "")
    if not hmac.compare_digest(token, integration.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    payload = json.loads(body)
    object_attr = payload.get("object_attributes", {})
    project = payload.get("project", {})

    if object_attr.get("action") in ("open", "create") or object_attr.get("state") == "opened":
        item = FeedbackItem(
            tenant_id=integration.tenant_id,
            source="gitlab",
            external_id=f"{project.get('path_with_namespace', '')}#{object_attr.get('iid')}",
            title=object_attr.get("title", ""),
            body=object_attr.get("description", ""),
            author_handle=payload.get("user", {}).get("username"),
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        start_feedback_pipeline.delay(item.id, integration.tenant_id)

    return {"success": True, "message": {"received": True}}


@router.post("/jira")
async def jira_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    integration = await _get_integration(request, "jira", db)

    body = await _read_body(request)

    # FIX #4 — Jira webhook now verifies a shared secret token
    token = request.headers.get("X-Jira-Token", "")
    if not hmac.compare_digest(token, integration.webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    payload = json.loads(body)
    issue = payload.get("issue", {})

    if issue:
        fields = issue.get("fields", {})
        item = FeedbackItem(
            tenant_id=integration.tenant_id,
            source="jira",
            external_id=issue.get("key", ""),
            title=fields.get("summary", ""),
            body=fields.get("description", ""),
            author_handle=fields.get("creator", {}).get("displayName"),
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        start_feedback_pipeline.delay(item.id, integration.tenant_id)

    return {"success": True, "message": {"received": True}}
