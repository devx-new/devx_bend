import asyncio
import re
from celery import shared_task
from sqlalchemy import select

from app.database import async_session_factory, run_in_celery
from app.models.feedback import FeedbackItem
from app.models.audit import AuditLog


async def _normalize_feedback_async(feedback_item_id: str, tenant_id: str) -> dict:
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

        original_body = item.body or ""

        # Strip the Priority/Sentiment header our pipeline injects into GitHub issue bodies
        normalized_body = re.sub(
            r"^\s*\*\*Priority:\*\*[^\n]*\n\*\*Sentiment:\*\*[^\n]*\n+",
            "",
            original_body,
            flags=re.IGNORECASE,
        ).strip()

        # Strip markdown headings (## Title, ### Section)
        normalized_body = re.sub(r"^#{1,6}\s+", "", normalized_body, flags=re.MULTILINE)

        # Strip markdown bold/italic (**text**, *text*, __text__, _text_)
        normalized_body = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", normalized_body)
        normalized_body = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", normalized_body)

        # Strip markdown links [text](url) → text
        normalized_body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", normalized_body)

        # Strip inline code `code` → code
        normalized_body = re.sub(r"`([^`]+)`", r"\1", normalized_body)

        # Strip fenced code blocks ```...```
        normalized_body = re.sub(r"```[\s\S]*?```", "", normalized_body)

        # Strip HTML tags
        normalized_body = re.sub(r'<[^>]+>', '', normalized_body)

        # Collapse whitespace
        normalized_body = " ".join(normalized_body.split())
        
        item.body = normalized_body
        
        # Audit Log
        audit = AuditLog(
            tenant_id=tenant_id,
            actor_id="system",
            action="NORMALIZE",
            resource_type="feedback_item",
            resource_id=item.id,
            diff={"before": {"body": original_body}, "after": {"body": normalized_body}},
            ip="127.0.0.1"
        )
        session.add(audit)
        
        await session.commit()
        return {"feedback_item_id": feedback_item_id, "tenant_id": tenant_id, "status": "normalized"}


@shared_task(bind=True, max_retries=3)
def normalize_feedback(self, feedback_item_id: str, tenant_id: str) -> dict:
    """Normalize incoming text, strip HTML, and prepare it for embedding."""
    return run_in_celery(_normalize_feedback_async(feedback_item_id, tenant_id))
