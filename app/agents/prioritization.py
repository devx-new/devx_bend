import logging

from celery import shared_task
from openai import OpenAI
from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory, run_in_celery
from app.models.audit import AuditLog
from app.models.feedback import FeedbackItem

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_PRIORITY_MODEL = "meta/llama-3.1-8b-instruct"


def _get_nvidia_client() -> OpenAI:
    return OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=settings.nvidia_api_key,
    )


async def _calculate_priority_async(feedback_item_id: str, tenant_id: str) -> dict:
    async with async_session_factory() as session:
        result = await session.execute(
            select(FeedbackItem).where(
                FeedbackItem.id == feedback_item_id,
                FeedbackItem.tenant_id == tenant_id,
            )
        )
        item = result.scalar_one_or_none()
        if not item or not item.body:
            return {"error": "Feedback not found or empty"}

        prompt = (
            "You are a triage assistant for a developer feedback platform. "
            "Assign a priority score from 0 to 100 based on urgency and impact, "
            "where >80 = critical, >60 = high, 40-60 = medium, <40 = low.\n\n"
            f"Title: {item.title}\n"
            f"Feedback: {item.body}\n"
            f"Sentiment (-1.0 to 1.0): {item.sentiment_score}\n"
            f"Category: {item.category}\n\n"
            "Output ONLY the integer score, nothing else."
        )

        priority = 50.0  # safe default (medium)
        model_used = "default"

        # Primary: NVIDIA NIM
        if settings.nvidia_api_key:
            try:
                client = _get_nvidia_client()
                response = client.chat.completions.create(
                    model=NVIDIA_PRIORITY_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=8,
                )
                score_text = response.choices[0].message.content.strip()
                # Strip any non-numeric characters (model might say "75." or "75/100")
                numeric = "".join(c for c in score_text if c.isdigit() or c == ".")
                priority = max(0.0, min(100.0, float(numeric)))
                model_used = NVIDIA_PRIORITY_MODEL
                logger.info(
                    "Priority score=%.1f for item=%s via NVIDIA NIM", priority, item.id
                )
            except Exception as exc:
                logger.warning(
                    "NVIDIA priority failed for item=%s: %s — falling back", item.id, exc
                )

        # Fallback: Gemini
        if model_used == "default" and settings.gemini_api_key:
            try:
                from google import genai
                from google.genai import types as gtypes

                g_client = genai.Client(api_key=settings.gemini_api_key)
                g_response = g_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(temperature=0.1),
                )
                score_text = g_response.text.strip()
                numeric = "".join(c for c in score_text if c.isdigit() or c == ".")
                priority = max(0.0, min(100.0, float(numeric)))
                model_used = "gemini-2.5-flash"
                logger.info(
                    "Priority score=%.1f for item=%s via Gemini fallback", priority, item.id
                )
            except Exception as exc:
                logger.error(
                    "Gemini priority fallback also failed for item=%s: %s", item.id, exc
                )

        if priority > 80:
            logger.warning("Critical priority=%.1f for item=%s", priority, item.id)

        old_priority = item.priority_score
        item.priority_score = round(priority, 1)

        audit = AuditLog(
            tenant_id=tenant_id,
            actor_id="system",
            action="PRIORITIZE",
            resource_type="feedback_item",
            resource_id=item.id,
            diff={
                "before": {"priority_score": old_priority},
                "after": {"priority_score": item.priority_score},
            },
            ip="127.0.0.1",
        )
        session.add(audit)

        await session.commit()
        return {
            "feedback_item_id": feedback_item_id,
            "tenant_id": tenant_id,
            "status": "prioritized",
        }


@shared_task(bind=True, max_retries=3)
def calculate_priority(self, previous_result: dict) -> dict:
    """Stage 6 — Priority score 0-100 via NVIDIA NIM (Llama 3.1 8B), Gemini fallback.
    >80 = critical, >60 = high. Logs critical items.
    """
    if "error" in previous_result:
        return previous_result
    return run_in_celery(
        _calculate_priority_async(
            previous_result["feedback_item_id"], previous_result["tenant_id"]
        )
    )
