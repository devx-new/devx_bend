import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.relevance import is_feedback_relevant, is_noise_by_rules
from app.config import settings
from app.core.notifications import notify_feedback_resolved
from app.database import get_db
from app.models.feedback import FeedbackItem
from app.models.integration import Integration
from app.worker.orchestrator import start_feedback_pipeline

logger = logging.getLogger(__name__)

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
        issue_labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
        if "dfp-automated" not in issue_labels:
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


@router.post("/github/{tenant_id}")
async def github_webhook_tenant(tenant_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Per-tenant GitHub webhook endpoint.
    GitHub sends issue events here; the tenant is identified via the URL path
    rather than a custom header that GitHub cannot add.
    Register this URL when configuring the GitHub integration.
    """
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "github",
            Integration.status == "active",
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=400, detail="No active GitHub integration for this tenant")

    secret = integration.webhook_secret or (integration.credentials or {}).get("webhook_secret")
    if not secret:
        raise HTTPException(status_code=500, detail="GitHub integration webhook_secret not configured")

    body = await _read_body(request)
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_hmac(body, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event_type = request.headers.get("x-github-event", "")
    if event_type != "issues":
        return {"success": True, "message": {"received": True}}

    payload = json.loads(body)
    action = payload.get("action")
    issue = payload.get("issue", {})

    if action == "opened" and issue:
        # Skip issues our own pipeline created to prevent re-ingestion loops
        issue_labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
        if "dfp-automated" in issue_labels:
            return {"success": True, "message": {"received": True}}

        repo_name = payload.get("repository", {}).get("full_name", "")
        external_id = f"{repo_name}#{issue.get('number')}"
        existing = await db.scalar(
            select(FeedbackItem).where(
                FeedbackItem.tenant_id == tenant_id,
                FeedbackItem.source == "github",
                FeedbackItem.external_id == external_id,
            )
        )
        if not existing:
            item = FeedbackItem(
                tenant_id=tenant_id,
                source="github",
                external_id=external_id,
                title=issue.get("title", ""),
                body=issue.get("body", "") or "",
                author_handle=issue.get("user", {}).get("login"),
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            start_feedback_pipeline.delay(item.id, tenant_id)

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


# Jira status names that mean "done" across common workflows
_JIRA_RESOLVED_STATUSES = {
    "done", "closed", "resolved", "complete", "completed",
    "won't fix", "wont fix", "cancelled", "canceled",
}


@router.post("/jira/{tenant_id}")
async def jira_webhook_tenant(tenant_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Per-tenant Jira webhook.  Register this URL in your Jira project's webhook settings.
    Handles:
      - jira:issue_created  → ingest as new feedback
      - jira:issue_updated  → if status changed to resolved/done, close the DevX item
    """
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "jira",
            Integration.status == "active",
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=400, detail="No active Jira integration for this tenant")

    body = await _read_body(request)
    payload = json.loads(body)

    webhook_event = payload.get("webhookEvent", "")
    issue = payload.get("issue", {})
    if not issue:
        return {"success": True, "message": {"received": True}}

    issue_key = issue.get("key", "")
    fields = issue.get("fields", {})

    # ── Issue created: ingest as new feedback ─────────────────────────────────
    if webhook_event == "jira:issue_created":
        existing = await db.scalar(
            select(FeedbackItem).where(
                FeedbackItem.tenant_id == tenant_id,
                FeedbackItem.source == "jira",
                FeedbackItem.external_id == issue_key,
            )
        )
        if not existing:
            item = FeedbackItem(
                tenant_id=tenant_id,
                source="jira",
                external_id=issue_key,
                title=fields.get("summary", ""),
                body=(fields.get("description") or ""),
                author_handle=(fields.get("reporter") or {}).get("displayName"),
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            start_feedback_pipeline.delay(item.id, tenant_id)

    # ── Issue updated: sync status back to DevX ───────────────────────────────
    elif webhook_event == "jira:issue_updated":
        changelog = payload.get("changelog", {})
        status_change = next(
            (c for c in changelog.get("items", []) if c.get("field") == "status"),
            None,
        )
        if not status_change:
            return {"success": True, "message": {"received": True}}

        new_status = (status_change.get("toString") or "").lower()
        if new_status not in _JIRA_RESOLVED_STATUSES:
            return {"success": True, "message": {"received": True}}

        # Find the DevX feedback item that spawned this Jira ticket
        feedback_item = await db.scalar(
            select(FeedbackItem).where(
                FeedbackItem.tenant_id == tenant_id,
                FeedbackItem.jira_issue_key == issue_key,
            )
        )
        if feedback_item and feedback_item.status not in ("resolved", "wont_fix"):
            devx_status = "wont_fix" if "fix" in new_status or "cancel" in new_status else "resolved"
            feedback_item.status = devx_status
            feedback_item.resolved_at = datetime.now(timezone.utc)
            await db.commit()
            await notify_feedback_resolved(feedback_item, db)

    return {"success": True, "message": {"received": True}}


# ── ClickUp Webhooks ─────────────────────────────────────────────────────────

_CLICKUP_RESOLVED_STATUSES = {"complete", "closed", "cancelled", "canceled", "done"}


@router.post("/clickup/{tenant_id}")
async def clickup_webhook_tenant(tenant_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Per-tenant ClickUp webhook.  Register this URL in ClickUp → Settings → Integrations → Webhooks.
    Handles taskStatusUpdated: when a ClickUp task is marked complete/closed, the linked
    DevX feedback item is resolved.
    """
    result = await db.execute(
        select(Integration).where(
            Integration.tenant_id == tenant_id,
            Integration.provider == "clickup",
            Integration.status == "active",
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=400, detail="No active ClickUp integration for this tenant")

    body = await _read_body(request)

    # Verify signature only if a webhook secret is stored
    webhook_secret = (integration.credentials or {}).get("webhook_secret")
    if webhook_secret:
        signature = request.headers.get("X-Signature", "")
        expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid ClickUp webhook signature")

    payload = json.loads(body)
    event = payload.get("event", "")
    task_id = payload.get("task_id", "")

    if event == "taskStatusUpdated" and task_id:
        new_status = ""
        for hi in payload.get("history_items", []):
            if hi.get("field") == "status":
                after = hi.get("after", {})
                new_status = (after.get("status") or after.get("type") or "").lower()
                break

        if new_status in _CLICKUP_RESOLVED_STATUSES:
            feedback_item = await db.scalar(
                select(FeedbackItem).where(
                    FeedbackItem.tenant_id == tenant_id,
                    FeedbackItem.clickup_task_id == task_id,
                )
            )
            if feedback_item and feedback_item.status not in ("resolved", "wont_fix"):
                devx_status = "wont_fix" if "cancel" in new_status else "resolved"
                feedback_item.status = devx_status
                feedback_item.resolved_at = datetime.now(timezone.utc)
                await db.commit()
                await notify_feedback_resolved(feedback_item, db)

    return {"success": True, "message": {"received": True}}


# ── Discord Interactions ──────────────────────────────────────────────────────

def _verify_discord_signature(public_key_hex: str, timestamp: str, body: bytes, signature_hex: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode() + body)
        return True
    except (InvalidSignature, ValueError):
        return False


@router.post("/discord")
async def discord_interactions(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Discord Interactions endpoint.
    Set this URL as the Interactions Endpoint URL in your Discord app settings.
    Handles the PING verification challenge and APPLICATION_COMMAND interactions.

    Slash command /report title:<text> description:<text> creates a FeedbackItem.
    """
    body = await _read_body(request)
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    signature = request.headers.get("X-Signature-Ed25519", "")

    if settings.discord_public_key and not _verify_discord_signature(
        settings.discord_public_key, timestamp, body, signature
    ):
        raise HTTPException(status_code=401, detail="Invalid Discord signature")

    payload = json.loads(body)
    interaction_type = payload.get("type")

    # Discord requires this to verify the endpoint during setup
    if interaction_type == 1:
        return {"type": 1}

    # APPLICATION_COMMAND — a slash command invocation
    if interaction_type == 2:
        data = payload.get("data", {})
        options = {o["name"]: o["value"] for o in data.get("options", [])}

        user = (payload.get("member") or {}).get("user") or payload.get("user") or {}
        username = user.get("username", "unknown")
        guild_id = payload.get("guild_id", "")

        title = options.get("title") or data.get("name", "Discord Feedback")
        body_text = options.get("description") or options.get("body") or title

        result = await db.execute(
            select(Integration).where(
                Integration.provider == "discord",
                Integration.status == "active",
            )
        )
        integration = None
        for row in result.scalars().all():
            if (row.credentials or {}).get("guild_id") == guild_id:
                integration = row
                break

        if not integration:
            return {"type": 4, "data": {"content": "This server is not connected to DevX."}}

        external_id = f"discord-{guild_id}-{payload.get('id', '')}"
        existing = await db.scalar(
            select(FeedbackItem).where(
                FeedbackItem.tenant_id == integration.tenant_id,
                FeedbackItem.source == "discord",
                FeedbackItem.external_id == external_id,
            )
        )
        if not existing:
            item = FeedbackItem(
                tenant_id=integration.tenant_id,
                source="discord",
                external_id=external_id,
                title=title[:200],
                body=body_text,
                author_handle=username,
            )
            db.add(item)
            await db.commit()
            await db.refresh(item)
            start_feedback_pipeline.delay(item.id, integration.tenant_id)

        return {"type": 4, "data": {"content": "Thanks for your feedback! We'll review it shortly."}}

    return {"type": 1}


# ── Slack Events API ──────────────────────────────────────────────────────────

def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Verify Slack request using the signing secret (prevents spoofed events)."""
    if not settings.slack_signing_secret:
        return False
    if not timestamp or abs(time.time() - int(timestamp)) > 300:
        return False
    base = f"v0:{timestamp}:{body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        settings.slack_signing_secret.encode("utf-8"),
        base.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/slack")
async def slack_events(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Slack Events API endpoint.
    Handles url_verification challenge + message.channels / app_mention events.

    Setup in your Slack app:
      Event Subscriptions → Request URL → https://<your-domain>/v1/webhooks/slack
      Subscribe to bot events: message.channels, message.groups, app_mention
    """
    body = await _read_body(request)
    payload = json.loads(body)

    # Handle URL verification BEFORE signature check — Slack sends this once
    # during setup to confirm the endpoint is reachable; it has no signature.
    if payload.get("type") == "url_verification":
        return Response(content=payload["challenge"], media_type="text/plain")

    # All real event callbacks must carry a valid signature
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if settings.slack_signing_secret and not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    # Ignore Slack retries to prevent duplicate feedback items
    if request.headers.get("X-Slack-Retry-Reason") == "http_timeout":
        return {"success": True, "message": {"received": True}}

    event = payload.get("event", {})
    event_type = event.get("type", "")

    # Skip bot messages and edits
    if event.get("bot_id") or event.get("subtype"):
        return {"success": True, "message": {"received": True}}

    if event_type not in ("message", "app_mention"):
        return {"success": True, "message": {"received": True}}

    text = (event.get("text") or "").strip()
    if not text:
        return {"success": True, "message": {"received": True}}

    if is_noise_by_rules({"from": event.get("user", ""), "label_ids": [], "body": text}):
        return {"success": True, "message": {"received": True}}

    team_id = payload.get("team_id", "")

    # Resolve tenant by matching team_id stored in integration credentials
    result = await db.execute(
        select(Integration).where(
            Integration.provider == "slack",
            Integration.status == "active",
        )
    )
    integration = None
    for row in result.scalars().all():
        if (row.credentials or {}).get("team_id") == team_id:
            integration = row
            break

    if not integration:
        return {"success": True, "message": {"received": True}}

    channel = event.get("channel", "unknown")
    ts = event.get("ts", "")
    user = event.get("user", "unknown")
    external_id = f"slack-{channel}-{ts}"

    # Deduplicate: skip if this message was already ingested
    existing = await db.scalar(
        select(FeedbackItem).where(
            FeedbackItem.tenant_id == integration.tenant_id,
            FeedbackItem.source == "slack",
            FeedbackItem.external_id == external_id,
        )
    )
    if existing:
        return {"success": True, "message": {"received": True}}

    if not await is_feedback_relevant(subject="", body=text, sender=user):
        logger.info(f"Skipping Slack message {external_id} (classified as non-feedback)")
        return {"success": True, "message": {"received": True}}

    item = FeedbackItem(
        tenant_id=integration.tenant_id,
        source="slack",
        external_id=external_id,
        title=text[:200],
        body=text,
        author_handle=user,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    start_feedback_pipeline.delay(item.id, integration.tenant_id)

    return {"success": True, "message": {"received": True}}
