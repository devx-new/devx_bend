import asyncio
import httpx
import logging
import re
from celery import shared_task
from sqlalchemy import select

from app.database import async_session_factory, run_in_celery
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

    # Only notify Slack for high-priority or critical-sentiment items to avoid noise
    priority = item.priority_score or 0
    sentiment = item.sentiment_score or 0
    if priority < 60 and sentiment > -0.6:
        logger.debug(
            "Skipping Slack for item=%s: priority=%.1f sentiment=%.2f below threshold",
            item.id, priority, sentiment,
        )
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
        with httpx.Client() as client:
            if webhook_url:
                # Incoming-webhook path (simple POST, no channel required)
                resp = client.post(webhook_url, json={"text": text}, timeout=10.0)
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
                    resp = client.post(
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
    if item.category not in ("bug", "feature", "performance", "security"):
        return False
    if item.priority_score is None:
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
    sentiment_str = f"{item.sentiment_score:.2f}" if item.sentiment_score is not None else "N/A"
    payload = {
        "title": f"[{item.category.upper()}] {item.title}",
        "body": (
            f"**Priority:** {item.priority_score:.1f}/100\n"
            f"**Sentiment:** {sentiment_str}\n\n"
            f"{item.body}"
        ),
        "labels": [item.category, "dfp-automated"],
    }

    created_urls = []
    try:
        with httpx.Client() as client:
            for repo in repo_names:
                resp = client.post(
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


# Preferred issue type names in priority order, per feedback category
_JIRA_PREFERRED_TYPES = {
    "bug":      ["Bug", "Defect", "Issue", "Task"],
    "feature":  ["Story", "Feature", "Task", "Issue"],
    "security": ["Bug", "Defect", "Task", "Issue"],
    "performance": ["Bug", "Task", "Issue"],
}
_JIRA_DEFAULT_PREFERENCE = ["Task", "Story", "Bug", "Issue", "Subtask"]


def _pick_issue_type(category: str | None, available: list[str]) -> str:
    """Return the best matching issue type name from those available in the project."""
    available_lower = {n.lower(): n for n in available}
    preferences = _JIRA_PREFERRED_TYPES.get(category or "", _JIRA_DEFAULT_PREFERENCE)
    for preferred in preferences:
        if preferred.lower() in available_lower:
            return available_lower[preferred.lower()]
    # Last resort: first non-subtask type the project offers
    return available[0] if available else "Task"


_DFP_META_PREFIX = re.compile(
    r"^\s*\*\*Priority:\*\*[^\n]*\n\*\*Sentiment:\*\*[^\n]*\n+",
    re.IGNORECASE,
)

def _clean_body(text: str | None) -> str:
    """Strip the Priority/Sentiment header our pipeline injects into GitHub issue bodies."""
    if not text:
        return ""
    return _DFP_META_PREFIX.sub("", text).strip()


async def _route_to_jira(item: FeedbackItem, integration: Integration, session) -> bool:
    creds = integration.credentials or {}
    config = integration.config or {}

    token = creds.get("access_token")
    cloud_id = creds.get("cloud_id")
    project_keys = config.get("project_keys", [])

    if not token or not cloud_id or not project_keys:
        logger.warning("Jira integration incomplete for tenant=%s", item.tenant_id)
        return False

    priority_label = (
        "high-priority" if (item.priority_score and item.priority_score > 70) else "normal-priority"
    )

    description_text = _clean_body(item.body) or item.title or ""
    adf_description = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": description_text}],
            },
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"Source: {item.source} | "
                            f"Priority: {item.priority_score:.1f}/100 | "
                            f"Sentiment: {item.sentiment_score:.2f}"
                        ),
                        "marks": [{"type": "em"}],
                    }
                ],
            },
        ],
    }

    created_keys = []
    try:
        with httpx.Client() as client:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            base = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"

            for project_key in project_keys:
                # Fetch valid issue types for this project to avoid 400 on unknown type names
                meta_resp = client.get(
                    f"{base}/issue/createmeta/{project_key}/issuetypes",
                    headers=headers,
                    timeout=10.0,
                )
                if meta_resp.status_code == 200:
                    available = [
                        t["name"] for t in meta_resp.json().get("issueTypes", [])
                        if not t.get("subtask")
                    ]
                else:
                    available = []

                issue_type = _pick_issue_type(item.category, available)
                logger.info(
                    "Jira project=%s available types=%s chosen=%s",
                    project_key, available, issue_type,
                )

                resp = client.post(
                    f"{base}/issue",
                    headers=headers,
                    json={
                        "fields": {
                            "project": {"key": project_key},
                            "summary": item.title[:255],
                            "description": adf_description,
                            "issuetype": {"name": issue_type},
                            "labels": [item.source or "dfp", priority_label],
                        }
                    },
                    timeout=15.0,
                )
                data = resp.json()
                if resp.status_code not in (200, 201):
                    raise ValueError(f"Jira API error {resp.status_code}: {data}")
                jira_key = data.get("key")
                created_keys.append(jira_key)
                logger.info("Created Jira issue %s for feedback=%s", jira_key, item.id)
                # Store first key so the Jira webhook can look this item back up
                if jira_key and not item.jira_issue_key:
                    item.jira_issue_key = jira_key

        audit = AuditLog(
            tenant_id=item.tenant_id,
            actor_id="system",
            action="CREATE_JIRA_ISSUE",
            resource_type="feedback_item",
            resource_id=item.id,
            diff={"after": {"jira_keys": created_keys}},
            ip="127.0.0.1",
        )
        session.add(audit)
        return True
    except Exception as e:
        logger.error("Failed to create Jira issue for item=%s: %s", item.id, e)
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
            if integration.provider == item.source:
                # Never echo feedback back to the provider it came from
                continue
            if integration.provider in ("slack", "github"):
                # Slack and GitHub are inbound-only; outbound routing is disabled
                continue
            elif integration.provider == "jira":
                await _route_to_jira(item, integration, session)

        await session.commit()
        return {"feedback_item_id": feedback_item_id, "tenant_id": tenant_id, "status": "routed"}


@shared_task(bind=True, max_retries=3)
def route_feedback(self, previous_result: dict) -> dict:
    """Dispatch to integrations based on configurable rules."""
    if "error" in previous_result:
        return previous_result
    return run_in_celery(
        _route_feedback_async(previous_result["feedback_item_id"], previous_result["tenant_id"])
    )
