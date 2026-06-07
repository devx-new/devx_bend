import asyncio
import httpx
import logging
from celery import shared_task
from sqlalchemy import select

from app.database import async_session_factory
from app.models.feedback import FeedbackItem
from app.models.integration import Integration
from app.models.audit import AuditLog

logger = logging.getLogger(__name__)


async def _route_to_slack(item: FeedbackItem, integration: Integration, session) -> bool:
    creds = integration.credentials or {}
    config = integration.config or {}

    webhook_url = creds.get("webhook_url")
    # OAuth flow stores bot_token; fall back to access_token for older records
    bot_token = creds.get("bot_token") or creds.get("access_token")

    if not webhook_url and not bot_token:
        return False

    emoji = "🚨" if (item.priority_score and item.priority_score > 80) else "💬"
    text = (
        f"{emoji} *New Feedback:* {item.title}\n"
        f"*Category:* {item.category}\n"
        f"*Priority:* {item.priority_score:.1f}/100\n"
        f"*Sentiment:* {item.sentiment_score:.2f}\n"
        f"*Content:* {item.body[:200]}..."
    )

    try:
        async with httpx.AsyncClient() as client:
            if webhook_url:
                # Incoming-webhook path (simple POST, no channel required)
                resp = await client.post(webhook_url, json={"text": text}, timeout=10.0)
                resp.raise_for_status()
            else:
                # Bot-token path — post to every configured channel
                channel_ids = config.get("channel_ids", [])
                if not channel_ids:
                    logger.warning(
                        "Slack bot token present but no channels configured for tenant=%s",
                        item.tenant_id,
                    )
                    return False
                for channel in channel_ids:
                    resp = await client.post(
                        "https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {bot_token}"},
                        json={"channel": channel, "text": text},
                        timeout=10.0,
                    )
                    data = resp.json()
                    if not data.get("ok"):
                        raise ValueError(f"Slack API error: {data.get('error')}")

        audit = AuditLog(
            tenant_id=item.tenant_id,
            actor_id="system",
            action="NOTIFY_SLACK",
            resource_type="feedback_item",
            resource_id=item.id,
            diff={"after": {"status": "success"}},
            ip="127.0.0.1",
        )
        session.add(audit)
        return True
    except Exception as e:
        logger.error("Failed to route to Slack: %s", e)
        return False


async def _route_to_github(item: FeedbackItem, integration: Integration, session) -> bool:
    if not item.priority_score or item.priority_score < 60:
        return False
    if item.category not in ("bug", "feature"):
        return False

    creds = integration.credentials or {}
    config = integration.config or {}

    access_token = creds.get("access_token")
    # Prefer repo_names saved via /configure; fall back to legacy credentials.repo
    repo_names = config.get("repo_names") or (
        [creds["repo"]] if creds.get("repo") else []
    )

    if not access_token or not repo_names:
        return False

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {
        "title": f"[{item.category.upper()}] {item.title}",
        "body": (
            f"**Priority:** {item.priority_score:.1f}/100\n"
            f"**Sentiment:** {item.sentiment_score:.2f}\n\n"
            f"{item.body}"
        ),
        "labels": [item.category, "dfp-automated"],
    }

    created_urls = []
    try:
        async with httpx.AsyncClient() as client:
            for repo in repo_names:
                resp = await client.post(
                    f"https://api.github.com/repos/{repo}/issues",
                    headers=headers,
                    json=payload,
                    timeout=10.0,
                )
                resp.raise_for_status()
                created_urls.append(resp.json().get("html_url"))

        audit = AuditLog(
            tenant_id=item.tenant_id,
            actor_id="system",
            action="NOTIFY_GITHUB",
            resource_type="feedback_item",
            resource_id=item.id,
            diff={"after": {"status": "success", "issue_urls": created_urls}},
            ip="127.0.0.1",
        )
        session.add(audit)
        return True
    except Exception as e:
        logger.error("Failed to route to GitHub: %s", e)
        return False


async def _route_feedback_async(feedback_item_id: str, tenant_id: str) -> dict:
    async with async_session_factory() as session:
        result = await session.execute(
            select(FeedbackItem).where(
                FeedbackItem.id == feedback_item_id,
                FeedbackItem.tenant_id == tenant_id,
            )
        )
        item = result.scalar_one_or_none()
        if not item:
            return {"error": "Feedback not found"}

        integrations_result = await session.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.status == "active",
            )
        )
        integrations = integrations_result.scalars().all()

        for integration in integrations:
            if integration.provider == "slack":
                await _route_to_slack(item, integration, session)
            elif integration.provider == "github":
                await _route_to_github(item, integration, session)

        await session.commit()
        return {"feedback_item_id": feedback_item_id, "tenant_id": tenant_id, "status": "routed"}


@shared_task(bind=True, max_retries=3)
def route_feedback(self, previous_result: dict) -> dict:
    """Dispatch to integrations based on configurable rules."""
    if "error" in previous_result:
        return previous_result
    return asyncio.run(
        _route_feedback_async(previous_result["feedback_item_id"], previous_result["tenant_id"])
    )
