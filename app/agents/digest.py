import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from celery import shared_task
from sqlalchemy import select, desc
from google import genai
from google.genai import types

from app.database import async_session_factory
from app.models.tenant import Tenant
from app.models.feedback import FeedbackItem
from app.models.digest import WeeklyDigest
from app.config import settings

logger = logging.getLogger(__name__)

_PII_PATTERNS = [
    re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),  # emails
    re.compile(r'\b\+?[0-9]{10,14}\b'),  # phone numbers
]


def _strip_pii(text: str) -> str:
    """Remove known PII patterns before sending to LLM."""
    for pattern in _PII_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text


async def _generate_weekly_digest_async() -> dict:
    async with async_session_factory() as session:
        result = await session.execute(select(Tenant))
        tenants = result.scalars().all()

        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=7)

        for tenant in tenants:
            # Fetch top 100 feedback items from past 7 days sorted by priority
            items_result = await session.execute(
                select(FeedbackItem)
                .where(
                    FeedbackItem.tenant_id == tenant.id,
                    FeedbackItem.created_at >= period_start,
                    FeedbackItem.status != "duplicate",
                )
                .order_by(desc(FeedbackItem.priority_score))
                .limit(100)
            )
            items = items_result.scalars().all()

            if not items:
                logger.info(f"No feedback for tenant {tenant.id} this week, skipping digest.")
                continue

            # Build the prompt content, stripping PII
            items_text = "\n".join([
                f"- [{item.category or 'uncategorized'}] (Priority: {item.priority_score or 0:.0f}, "
                f"Sentiment: {item.sentiment_score or 0:.2f}): {_strip_pii(item.title)}"
                for item in items
            ])

            prompt = (
                f"You are a developer experience analyst. Below are the top {len(items)} feedback items "
                f"from the past week for a software team.\n\n"
                f"Feedback:\n{items_text}\n\n"
                f"Write a concise weekly digest report in Markdown format that includes:\n"
                f"1. Executive Summary (2-3 sentences)\n"
                f"2. Top Critical Issues (if any with priority > 80)\n"
                f"3. Top Feature Requests\n"
                f"4. Sentiment Overview\n"
                f"5. Recommended Actions\n"
            )

            try:
                client = genai.Client(api_key=settings.gemini_api_key)
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=prompt,
                    config=types.GenerateContentConfig(temperature=0.4)
                )
                report_markdown = response.text.strip()
            except Exception as e:
                logger.error(f"Gemini digest generation failed for tenant {tenant.id}: {e}")
                continue

            digest = WeeklyDigest(
                tenant_id=tenant.id,
                report_markdown=report_markdown,
                period_start=period_start,
                period_end=period_end,
            )
            session.add(digest)
            logger.info(f"Weekly digest generated for tenant {tenant.id}")

        await session.commit()
        return {"status": "success"}


async def _generate_digest_for_tenant_async(tenant_id: str) -> WeeklyDigest | None:
    """Generate and persist a digest for a single tenant. Returns the new WeeklyDigest or None."""
    async with async_session_factory() as session:
        period_end = datetime.now(timezone.utc)
        period_start = period_end - timedelta(days=7)

        items_result = await session.execute(
            select(FeedbackItem)
            .where(
                FeedbackItem.tenant_id == tenant_id,
                FeedbackItem.created_at >= period_start,
                FeedbackItem.status != "duplicate",
            )
            .order_by(desc(FeedbackItem.priority_score))
            .limit(100)
        )
        items = items_result.scalars().all()

        if not items:
            return None

        items_text = "\n".join([
            f"- [{item.category or 'uncategorized'}] (Priority: {item.priority_score or 0:.0f}, "
            f"Sentiment: {item.sentiment_score or 0:.2f}): {_strip_pii(item.title)}"
            for item in items
        ])
        prompt = (
            f"You are a developer experience analyst. Below are the top {len(items)} feedback items "
            f"from the past week for a software team.\n\n"
            f"Feedback:\n{items_text}\n\n"
            f"Write a concise weekly digest report in Markdown format that includes:\n"
            f"1. Executive Summary (2-3 sentences)\n"
            f"2. Top Critical Issues (if any with priority > 80)\n"
            f"3. Top Feature Requests\n"
            f"4. Sentiment Overview\n"
            f"5. Recommended Actions\n"
        )

        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.4),
        )
        digest = WeeklyDigest(
            tenant_id=tenant_id,
            report_markdown=response.text.strip(),
            period_start=period_start,
            period_end=period_end,
        )
        session.add(digest)
        await session.commit()
        await session.refresh(digest)
        return digest


@shared_task(bind=True, max_retries=3)
def generate_weekly_digest(self) -> dict:
    """Generate weekly summaries via Gemini API for all tenants."""
    return asyncio.run(_generate_weekly_digest_async())

