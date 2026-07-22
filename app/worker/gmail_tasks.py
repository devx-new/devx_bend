import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import gmail
from app.database import async_session_factory, run_in_celery
from app.models.feedback import FeedbackItem
from app.models.integration import Integration
from app.worker.celery_app import celery_app
from app.worker.orchestrator import start_feedback_pipeline

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK = timedelta(days=7)


async def _poll_integration(integration: Integration, session: AsyncSession) -> int:
    try:
        access_token, updated_credentials = await gmail.get_valid_access_token(integration.credentials or {})
    except ValueError as exc:
        logger.error(f"Gmail token refresh failed for tenant {integration.tenant_id}: {exc}")
        return 0
    if updated_credentials:
        integration.credentials = updated_credentials

    since = integration.last_sync_at or (datetime.now(timezone.utc) - _DEFAULT_LOOKBACK)
    base_query = (integration.config or {}).get("query", "").strip()
    query = " ".join(filter(None, [base_query, f"after:{int(since.timestamp())}"]))

    try:
        message_ids = await gmail.list_message_ids(access_token, query)
    except ValueError as exc:
        logger.error(f"Gmail message list failed for tenant {integration.tenant_id}: {exc}")
        await session.commit()  # persist any token refresh even if listing failed
        return 0

    ingested = 0
    for message_id in message_ids:
        existing = await session.scalar(
            select(FeedbackItem).where(
                FeedbackItem.tenant_id == integration.tenant_id,
                FeedbackItem.source == "gmail",
                FeedbackItem.external_id == message_id,
            )
        )
        if existing:
            continue

        try:
            message = await gmail.get_message(access_token, message_id)
        except ValueError as exc:
            logger.error(f"Gmail message fetch failed ({message_id}): {exc}")
            continue

        item = FeedbackItem(
            tenant_id=integration.tenant_id,
            source="gmail",
            external_id=message["id"],
            title=message["subject"][:200],
            body=message["body"],
            author_handle=message["from"],
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        start_feedback_pipeline.delay(item.id, integration.tenant_id)
        ingested += 1

    integration.last_sync_at = datetime.now(timezone.utc)
    await session.commit()
    return ingested


async def _poll_gmail_async() -> dict:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Integration).where(
                Integration.provider == "gmail",
                Integration.status == "active",
            )
        )
        integrations = result.scalars().all()

        ingested = 0
        for integration in integrations:
            ingested += await _poll_integration(integration, session)

        return {"status": "success", "tenants_polled": len(integrations), "items_ingested": ingested}


@celery_app.task(bind=True, max_retries=3)
def poll_gmail_feedback(self):
    """Periodic task: pulls new messages from every connected Gmail inbox into FeedbackItem rows."""
    return run_in_celery(_poll_gmail_async())
