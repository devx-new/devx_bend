import logging
from html import escape

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feedback import FeedbackItem
from app.models.tenant import Tenant
from app.worker.email_tasks import send_templated_email_task

logger = logging.getLogger(__name__)


async def notify_feedback_resolved(item: FeedbackItem, db: AsyncSession) -> None:
    """
    Queue a background email to the person who submitted a widget feedback item once
    it's resolved/won't-fixed — whether that happened via a Jira/ClickUp webhook or a
    manual status change in the DevX dashboard. Only widget submissions carry a
    submitter email (captured as `author_handle` in the widget's "Your email" field)
    — other sources (GitHub, Slack, Discord, etc.) store a display name/handle there
    instead, so skip those. The actual send happens in a Celery worker — see
    app.worker.email_tasks.
    """
    if item.source != "widget" or not item.author_handle:
        logger.info(
            "Skipping feedback-resolved email — not a widget submission or no submitter email",
            extra={"feedback_id": item.id, "source": item.source, "has_email": bool(item.author_handle)},
        )
        return

    # The status flip is already committed at this point and is idempotency-guarded
    # by callers (they skip re-notifying once status is already "resolved"/"wont_fix"),
    # so a failure here must never propagate — otherwise a 500 tells Jira/ClickUp to
    # retry the webhook, the retry no-ops on the already-resolved status, and the
    # submitter never gets notified at all.
    try:
        tenant = await db.get(Tenant, item.tenant_id)
        org_name = tenant.name if tenant else None

        wont_fix = item.status == "wont_fix"
        title = escape(item.title)
        team = f"The team at {escape(org_name)}" if org_name else "Our team"
        closing_line = (
            f"{team} reviewed this and won't be pursuing it right now — thanks for taking the time to share it."
            if wont_fix
            else f"{team} has resolved this. Thanks for helping us make things better!"
        )

        async_result = send_templated_email_task.delay(
            item.author_handle,
            f"Update on your feedback: {item.title[:60]}",
            "generic.html",
            org_name=org_name,
            heading="An update on your feedback" if wont_fix else "Your feedback has been resolved",
            body_paragraphs=[f'You reported: "{title}"', closing_line],
            details=[{"label": "Status", "value": "Won't fix" if wont_fix else "Resolved"}],
        )
        logger.info(
            "Queued feedback-resolved email",
            extra={"feedback_id": item.id, "task_id": async_result.id, "to": item.author_handle},
        )
    except Exception:
        logger.exception(
            "Failed to queue feedback-resolved email",
            extra={"feedback_id": item.id, "tenant_id": item.tenant_id},
        )
