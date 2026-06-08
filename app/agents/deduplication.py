import asyncio
from celery import shared_task
from sqlalchemy import select

from app.database import async_session_factory, run_in_celery
from app.models.feedback import FeedbackItem, DuplicateGroup


async def _dedup_feedback_async(feedback_item_id: str, tenant_id: str) -> dict:
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
        if not item.embedding:
            # Embedding unavailable (e.g. external API unreachable) — skip dedup, continue pipeline
            return {"feedback_item_id": feedback_item_id, "tenant_id": tenant_id, "status": "dedup_skipped"}

        # Perform pgvector cosine similarity search (>0.88 means distance < 0.12)
        # We search for items in the same tenant, excluding the current item itself.
        similarity_threshold = 0.12
        
        duplicate_query = select(FeedbackItem).where(
            FeedbackItem.tenant_id == tenant_id,
            FeedbackItem.id != feedback_item_id,
            FeedbackItem.embedding.cosine_distance(item.embedding) < similarity_threshold
        ).order_by(FeedbackItem.embedding.cosine_distance(item.embedding).asc()).limit(1)
        
        duplicate_result = await session.execute(duplicate_query)
        canonical_item = duplicate_result.scalar_one_or_none()
        
        if canonical_item:
            dup_group = DuplicateGroup(
                tenant_id=tenant_id,
                canonical_item_id=canonical_item.id
            )
            session.add(dup_group)
            
            item.status = "duplicate"
            await session.commit()
            return {"error": "Item is a duplicate", "canonical_id": canonical_item.id}
            
        return {"feedback_item_id": feedback_item_id, "tenant_id": tenant_id, "status": "deduplicated"}


@shared_task(bind=True, max_retries=3)
def dedup_feedback(self, previous_result: dict) -> dict:
    """Detect duplicates using cosine similarity (>0.88)."""
    if "error" in previous_result:
        return previous_result
    return run_in_celery(_dedup_feedback_async(previous_result["feedback_item_id"], previous_result["tenant_id"]))
