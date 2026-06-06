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
    webhook_url = integration.credentials.get("webhook_url") if integration.credentials else None
    if not webhook_url:
        return False
        
    emoji = "🚨" if (item.priority_score and item.priority_score > 80) else "💬"
    text = (
        f"{emoji} *New Feedback:* {item.title}\n"
        f"*Category:* {item.category}\n"
        f"*Priority:* {item.priority_score:.1f}/100\n"
        f"*Sentiment:* {item.sentiment_score:.2f}\n"
        f"*Content:* {item.body[:200]}..."
    )
    
    payload = {"text": text}
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=10.0)
            resp.raise_for_status()
            
            # Log Notification
            audit = AuditLog(
                tenant_id=item.tenant_id,
                actor_id="system",
                action="NOTIFY_SLACK",
                resource_type="feedback_item",
                resource_id=item.id,
                diff={"after": {"status": "success"}},
                ip="127.0.0.1"
            )
            session.add(audit)
            return True
    except Exception as e:
        logger.error(f"Failed to route to Slack: {e}")
        return False

async def _route_to_github(item: FeedbackItem, integration: Integration, session) -> bool:
    if not item.priority_score or item.priority_score < 60:
        return False # Only high priority
        
    if not item.category in ("bug", "feature"):
        return False # Only bugs and features
        
    access_token = integration.credentials.get("access_token") if integration.credentials else None
    repo = integration.credentials.get("repo") if integration.credentials else None
    
    if not access_token or not repo:
        return False
        
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    
    payload = {
        "title": f"[{item.category.upper()}] {item.title}",
        "body": f"**Priority:** {item.priority_score:.1f}\n**Sentiment:** {item.sentiment_score:.2f}\n\n{item.body}",
        "labels": [item.category, "dfp-automated"]
    }
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=10.0)
            resp.raise_for_status()
            
            # Log Notification
            audit = AuditLog(
                tenant_id=item.tenant_id,
                actor_id="system",
                action="NOTIFY_GITHUB",
                resource_type="feedback_item",
                resource_id=item.id,
                diff={"after": {"status": "success", "issue_url": resp.json().get("html_url")}},
                ip="127.0.0.1"
            )
            session.add(audit)
            return True
    except Exception as e:
        logger.error(f"Failed to route to GitHub: {e}")
        return False

async def _route_feedback_async(feedback_item_id: str, tenant_id: str) -> dict:
    async with async_session_factory() as session:
        result = await session.execute(
            select(FeedbackItem).where(
                FeedbackItem.id == feedback_item_id,
                FeedbackItem.tenant_id == tenant_id
            )
        )
        item = result.scalar_one_or_none()
        if not item:
            return {"error": "Feedback not found"}

        integrations_result = await session.execute(
            select(Integration).where(
                Integration.tenant_id == tenant_id,
                Integration.status == "active"
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
    return asyncio.run(_route_feedback_async(previous_result["feedback_item_id"], previous_result["tenant_id"]))
