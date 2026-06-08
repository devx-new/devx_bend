import asyncio
import httpx
import logging
import re
from celery import shared_task
from sqlalchemy import select

from app.database import async_session_factory, run_in_celery
from app.models.feedback import FeedbackItem
from app.models.integration import Integration
from app.models.routing import RoutingRule
from app.models.audit import AuditLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Condition matching
# ---------------------------------------------------------------------------

def _matches_condition(item: FeedbackItem, condition: dict) -> bool:
    """Return True if the feedback item satisfies all conditions in the rule."""
    if not condition:
        return True

    source = condition.get("source")
    if source and source != "any" and item.source != source:
        return False

    category = condition.get("category")
    if category and category != "any" and item.category != category:
        return False

    priority_min = condition.get("priority_min")
    if priority_min is not None:
        if item.priority_score is None or item.priority_score < float(priority_min):
            return False

    sentiment_max = condition.get("sentiment_max")
    if sentiment_max is not None:
        if item.sentiment_score is None or item.sentiment_score > float(sentiment_max):
            return False

    return True


# ---------------------------------------------------------------------------
# Destination handlers
# ---------------------------------------------------------------------------

async def _route_to_slack(item: FeedbackItem, integration: Integration, session, channel_override: str | None = None) -> bool:
    creds = integration.credentials or {}
    config = integration.config or {}

    webhook_url = creds.get("webhook_url")
    bot_token = creds.get("bot_token") or creds.get("access_token")

    if not webhook_url and not bot_token:
        return False

    emoji = "🚨" if (item.priority_score and item.priority_score > 80) else "💬"
    text = (
        f"{emoji} *New Feedback:* {item.title}\n"
        f"*Category:* {item.category or 'uncategorized'}\n"
        f"*Priority:* {item.priority_score:.1f}/100\n"
        f"*Sentiment:* {item.sentiment_score:.2f}\n"
        f"*Content:* {(item.body or '')[:200]}..."
    )

    try:
        with httpx.Client() as client:
            if channel_override and bot_token:
                resp = client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {bot_token}"},
                    json={"channel": channel_override, "text": text},
                    timeout=10.0,
                )
                data = resp.json()
                if not data.get("ok"):
                    raise ValueError(f"Slack API error: {data.get('error')}")
            elif webhook_url:
                resp = client.post(webhook_url, json={"text": text}, timeout=10.0)
                resp.raise_for_status()
            else:
                channel_ids = config.get("channel_ids", [])
                if not channel_ids:
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
            tenant_id=item.tenant_id, actor_id="system", action="NOTIFY_SLACK",
            resource_type="feedback_item", resource_id=item.id,
            diff={"after": {"status": "success", "channel": channel_override}},
            ip="127.0.0.1",
        )
        session.add(audit)
        return True
    except Exception as e:
        logger.error("Failed to route to Slack: %s", e)
        return False


async def _route_to_github(item: FeedbackItem, integration: Integration, session, repo_override: str | None = None) -> bool:
    if item.category not in ("bug", "feature", "performance", "security"):
        return False

    creds = integration.credentials or {}
    config = integration.config or {}
    access_token = creds.get("access_token")
    repo_names = ([repo_override] if repo_override else None) or config.get("repo_names") or (
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
                    headers=headers, json=payload, timeout=10.0,
                )
                resp.raise_for_status()
                created_urls.append(resp.json().get("html_url"))

        audit = AuditLog(
            tenant_id=item.tenant_id, actor_id="system", action="NOTIFY_GITHUB",
            resource_type="feedback_item", resource_id=item.id,
            diff={"after": {"status": "success", "issue_urls": created_urls}},
            ip="127.0.0.1",
        )
        session.add(audit)
        return True
    except Exception as e:
        logger.error("Failed to route to GitHub: %s", e)
        return False


_JIRA_PREFERRED_TYPES = {
    "bug":         ["Bug", "Defect", "Issue", "Task"],
    "feature":     ["Story", "Feature", "Task", "Issue"],
    "security":    ["Bug", "Defect", "Task", "Issue"],
    "performance": ["Bug", "Task", "Issue"],
}
_JIRA_DEFAULT_PREFERENCE = ["Task", "Story", "Bug", "Issue", "Subtask"]

_DFP_META_PREFIX = re.compile(
    r"^\s*\*\*Priority:\*\*[^\n]*\n\*\*Sentiment:\*\*[^\n]*\n+",
    re.IGNORECASE,
)


def _pick_issue_type(category: str | None, available: list[str]) -> str:
    available_lower = {n.lower(): n for n in available}
    preferences = _JIRA_PREFERRED_TYPES.get(category or "", _JIRA_DEFAULT_PREFERENCE)
    for preferred in preferences:
        if preferred.lower() in available_lower:
            return available_lower[preferred.lower()]
    return available[0] if available else "Task"


def _clean_body(text: str | None) -> str:
    if not text:
        return ""
    return _DFP_META_PREFIX.sub("", text).strip()


async def _route_to_jira(item: FeedbackItem, integration: Integration, session, project_key_override: str | None = None) -> bool:
    creds = integration.credentials or {}
    config = integration.config or {}

    token = creds.get("access_token")
    cloud_id = creds.get("cloud_id")
    project_keys = ([project_key_override] if project_key_override else None) or config.get("project_keys", [])

    if not token or not cloud_id or not project_keys:
        logger.warning("Jira integration incomplete for tenant=%s", item.tenant_id)
        return False

    priority_label = "high-priority" if (item.priority_score and item.priority_score > 70) else "normal-priority"
    description_text = _clean_body(item.body) or item.title or ""
    adf_description = {
        "type": "doc", "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": description_text}]},
            {"type": "paragraph", "content": [{"type": "text", "text": (
                f"Source: {item.source} | "
                f"Priority: {item.priority_score:.1f}/100 | "
                f"Sentiment: {item.sentiment_score:.2f}"
            ), "marks": [{"type": "em"}]}]},
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
                meta_resp = client.get(
                    f"{base}/issue/createmeta/{project_key}/issuetypes",
                    headers=headers, timeout=10.0,
                )
                available = [
                    t["name"] for t in meta_resp.json().get("issueTypes", [])
                    if not t.get("subtask")
                ] if meta_resp.status_code == 200 else []
                issue_type = _pick_issue_type(item.category, available)

                resp = client.post(
                    f"{base}/issue",
                    headers=headers,
                    json={"fields": {
                        "project": {"key": project_key},
                        "summary": item.title[:255],
                        "description": adf_description,
                        "issuetype": {"name": issue_type},
                        "labels": [item.source or "dfp", priority_label],
                    }},
                    timeout=15.0,
                )
                data = resp.json()
                if resp.status_code not in (200, 201):
                    raise ValueError(f"Jira API error {resp.status_code}: {data}")
                jira_key = data.get("key")
                created_keys.append(jira_key)
                if jira_key and not item.jira_issue_key:
                    item.jira_issue_key = jira_key

        audit = AuditLog(
            tenant_id=item.tenant_id, actor_id="system", action="CREATE_JIRA_ISSUE",
            resource_type="feedback_item", resource_id=item.id,
            diff={"after": {"jira_keys": created_keys}},
            ip="127.0.0.1",
        )
        session.add(audit)
        return True
    except Exception as e:
        logger.error("Failed to create Jira issue for item=%s: %s", item.id, e)
        return False


async def _route_to_linear(item: FeedbackItem, integration: Integration, session, team_id_override: str | None = None) -> bool:
    creds = integration.credentials or {}
    config = integration.config or {}
    token = creds.get("access_token")
    if not token:
        return False

    team_ids = ([team_id_override] if team_id_override else None) or config.get("team_ids", [])
    if not team_ids:
        logger.warning("Linear integration has no teams configured for tenant=%s", item.tenant_id)
        return False

    score = item.priority_score or 0
    linear_priority = 4
    if score >= 80:
        linear_priority = 1
    elif score >= 60:
        linear_priority = 2
    elif score >= 30:
        linear_priority = 3

    created_ids = []
    try:
        async with httpx.AsyncClient() as client:
            for team_id in team_ids:
                mutation = """
                mutation CreateIssue($teamId: String!, $title: String!, $description: String, $priority: Int) {
                    issueCreate(input: {teamId: $teamId, title: $title, description: $description, priority: $priority}) {
                        success
                        issue { id identifier url }
                    }
                }
                """
                resp = await client.post(
                    "https://api.linear.app/graphql",
                    json={
                        "query": mutation,
                        "variables": {
                            "teamId": team_id,
                            "title": item.title[:255],
                            "description": (
                                f"**Source:** {item.source}\n"
                                f"**Priority Score:** {item.priority_score:.1f}/100\n"
                                f"**Sentiment:** {item.sentiment_score:.2f}\n\n"
                                f"{_clean_body(item.body)}"
                            ),
                            "priority": linear_priority,
                        },
                    },
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    timeout=15.0,
                )
                data = resp.json()
                issue = data.get("data", {}).get("issueCreate", {}).get("issue", {})
                if issue.get("id"):
                    created_ids.append(issue["identifier"])

        audit = AuditLog(
            tenant_id=item.tenant_id, actor_id="system", action="CREATE_LINEAR_ISSUE",
            resource_type="feedback_item", resource_id=item.id,
            diff={"after": {"linear_ids": created_ids}},
            ip="127.0.0.1",
        )
        session.add(audit)
        return True
    except Exception as e:
        logger.error("Failed to create Linear issue for item=%s: %s", item.id, e)
        return False


# ---------------------------------------------------------------------------
# Rule executor
# ---------------------------------------------------------------------------

async def _execute_rule(item: FeedbackItem, rule: RoutingRule, integrations_by_provider: dict, session) -> bool:
    action_type = rule.action_type
    action_config = rule.action_config or {}

    if action_type == "post_slack":
        integration = integrations_by_provider.get("slack")
        if not integration:
            logger.warning("Rule %s: Slack not connected", rule.id)
            return False
        return await _route_to_slack(item, integration, session, channel_override=action_config.get("channel_id"))

    elif action_type == "create_jira_issue":
        integration = integrations_by_provider.get("jira")
        if not integration:
            logger.warning("Rule %s: Jira not connected", rule.id)
            return False
        return await _route_to_jira(item, integration, session, project_key_override=action_config.get("project_key"))

    elif action_type == "create_github_issue":
        integration = integrations_by_provider.get("github")
        if not integration:
            logger.warning("Rule %s: GitHub not connected", rule.id)
            return False
        return await _route_to_github(item, integration, session, repo_override=action_config.get("repo"))

    elif action_type == "create_linear_issue":
        integration = integrations_by_provider.get("linear")
        if not integration:
            logger.warning("Rule %s: Linear not connected", rule.id)
            return False
        return await _route_to_linear(item, integration, session, team_id_override=action_config.get("team_id"))

    logger.warning("Unknown action_type '%s' in rule %s", action_type, rule.id)
    return False


# ---------------------------------------------------------------------------
# Main entry point (called by Celery chain)
# ---------------------------------------------------------------------------

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
        integrations_by_provider = {i.provider: i for i in integrations_result.scalars().all()}

        rules_result = await session.execute(
            select(RoutingRule).where(
                RoutingRule.tenant_id == tenant_id,
                RoutingRule.enabled == True,  # noqa: E712
            )
        )
        rules = rules_result.scalars().all()

        routed_count = 0
        if rules:
            for rule in rules:
                if _matches_condition(item, rule.condition):
                    ok = await _execute_rule(item, rule, integrations_by_provider, session)
                    if ok:
                        routed_count += 1
        else:
            # Fallback: auto-route to Jira when no rules are configured
            jira = integrations_by_provider.get("jira")
            if jira and item.source != "jira":
                ok = await _route_to_jira(item, jira, session)
                if ok:
                    routed_count += 1

        await session.commit()
        return {
            "feedback_item_id": feedback_item_id,
            "tenant_id": tenant_id,
            "status": "routed",
            "rules_executed": routed_count,
        }


@shared_task(bind=True, max_retries=3)
def route_feedback(self, previous_result: dict) -> dict:
    """Dispatch to integrations based on configurable routing rules."""
    if "error" in previous_result:
        return previous_result
    return run_in_celery(
        _route_feedback_async(previous_result["feedback_item_id"], previous_result["tenant_id"])
    )
