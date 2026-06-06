import asyncio
import re
from celery import shared_task
from sqlalchemy import select

from app.database import async_session_factory
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
        
        # INGEST-003: Normalize data. Strip basic HTML tags.
        normalized_body = re.sub(r'<[^>]+>', '', original_body)
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
    return asyncio.run(_normalize_feedback_async(feedback_item_id, tenant_id))
