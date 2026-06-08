import logging

import httpx
from celery import shared_task
from openai import OpenAI
from sqlalchemy import select
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from app.config import settings
from app.database import async_session_factory, run_in_celery
from app.models.audit import AuditLog
from app.models.feedback import FeedbackItem, FeedbackTag

logger = logging.getLogger(__name__)

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_CLASSIFY_MODEL = "meta/llama-3.1-8b-instruct"  # lightweight for classify

CATEGORIES = ["bug", "feature", "docs", "performance", "security"]


def _get_nvidia_client() -> OpenAI:
    return OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=settings.nvidia_api_key,
    )


# ── Stage 2: Embed ─────────────────────────────────────────────────────────────

async def _embed_feedback_async(feedback_item_id: str, tenant_id: str) -> dict:
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

        api_url = (
            "https://api-inference.huggingface.co/pipeline/feature-extraction/"
            "sentence-transformers/all-MiniLM-L6-v2"
        )
        headers = {"Authorization": f"Bearer {settings.huggingface_api_key}"}

        try:
            with httpx.Client() as client:
                response = client.post(
                    api_url,
                    headers=headers,
                    json={"inputs": item.body},
                    timeout=15.0,
                )
                response.raise_for_status()
                embedding = response.json()
                if isinstance(embedding, list) and len(embedding) == 384:
                    item.embedding = embedding
                    logger.info(
                        "Generated embedding size=%d for item=%s", len(embedding), item.id
                    )
                else:
                    logger.warning("Unexpected embedding shape for item=%s", item.id)
        except Exception as exc:
            logger.error("Embed failed for item=%s: %s", item.id, exc)
            # Non-fatal: pipeline continues without embedding

        await session.commit()
        return {"feedback_item_id": feedback_item_id, "tenant_id": tenant_id, "status": "embedded"}


@shared_task(bind=True, max_retries=3)
def embed_feedback(self, previous_result: dict) -> dict:
    """Stage 2 — Generate 384-dimensional embeddings using all-MiniLM-L6-v2."""
    if "error" in previous_result:
        return previous_result
    return run_in_celery(
        _embed_feedback_async(previous_result["feedback_item_id"], previous_result["tenant_id"])
    )


# ── Stage 4: Classify ──────────────────────────────────────────────────────────

async def _classify_feedback_async(feedback_item_id: str, tenant_id: str) -> dict:
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

        prompt = (
            f"Classify the following developer feedback into exactly ONE of these categories: "
            f"{', '.join(CATEGORIES)}.\n\n"
            f"Feedback: {item.body}\n\n"
            f"Output ONLY the category name in lowercase, nothing else."
        )

        category = "uncategorized"
        confidence = 0.0
        model_used = "fallback"

        # Primary: NVIDIA NIM (Llama 3.1 8B — free tier)
        if settings.nvidia_api_key:
            try:
                client = _get_nvidia_client()
                response = client.chat.completions.create(
                    model=NVIDIA_CLASSIFY_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=16,
                )
                raw = response.choices[0].message.content.strip().lower()
                category = raw if raw in CATEGORIES else "uncategorized"
                confidence = 0.90
                model_used = NVIDIA_CLASSIFY_MODEL
                logger.info("Classified item=%s as '%s' via NVIDIA NIM", item.id, category)
            except Exception as exc:
                logger.warning("NVIDIA classify failed for item=%s: %s — falling back", item.id, exc)

        # Fallback: Gemini
        if model_used == "fallback" and settings.gemini_api_key:
            try:
                from google import genai
                from google.genai import types as gtypes

                g_client = genai.Client(api_key=settings.gemini_api_key)
                g_response = g_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(temperature=0.1),
                )
                raw = g_response.text.strip().lower()
                category = raw if raw in CATEGORIES else "uncategorized"
                confidence = 0.85
                model_used = "gemini-2.5-flash"
                logger.info("Classified item=%s as '%s' via Gemini fallback", item.id, category)
            except Exception as exc:
                logger.error("Gemini classify fallback also failed for item=%s: %s", item.id, exc)

        old_category = item.category
        item.category = category

        tag = FeedbackTag(
            feedback_item_id=item.id,
            tag=category,
            confidence_score=confidence,
            model_version=model_used,
        )
        session.add(tag)

        audit = AuditLog(
            tenant_id=tenant_id,
            actor_id="system",
            action="CLASSIFY",
            resource_type="feedback_item",
            resource_id=item.id,
            diff={"before": {"category": old_category}, "after": {"category": category}},
            ip="127.0.0.1",
        )
        session.add(audit)

        await session.commit()
        return {"feedback_item_id": feedback_item_id, "tenant_id": tenant_id, "status": "classified"}


@shared_task(bind=True, max_retries=3)
def classify_feedback(self, previous_result: dict) -> dict:
    """Stage 4 — Categorize using NVIDIA NIM (Llama 3.1 8B), Gemini as fallback."""
    if "error" in previous_result:
        return previous_result
    return run_in_celery(
        _classify_feedback_async(previous_result["feedback_item_id"], previous_result["tenant_id"])
    )


# ── Stage 5: Sentiment ─────────────────────────────────────────────────────────

async def _analyze_sentiment_async(feedback_item_id: str, tenant_id: str) -> dict:
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

        # VADER is fast, local, and well-suited for short developer feedback
        analyzer = SentimentIntensityAnalyzer()
        vs = analyzer.polarity_scores(item.body)
        sentiment = round(vs["compound"], 4)

        if sentiment < -0.6:
            logger.warning(
                "Critical negative sentiment %.3f for item=%s", sentiment, item.id
            )

        old_sentiment = item.sentiment_score
        item.sentiment_score = sentiment

        audit = AuditLog(
            tenant_id=tenant_id,
            actor_id="system",
            action="SENTIMENT",
            resource_type="feedback_item",
            resource_id=item.id,
            diff={
                "before": {"sentiment_score": old_sentiment},
                "after": {"sentiment_score": sentiment},
            },
            ip="127.0.0.1",
        )
        session.add(audit)

        await session.commit()
        return {
            "feedback_item_id": feedback_item_id,
            "tenant_id": tenant_id,
            "status": "sentiment_analyzed",
        }


@shared_task(bind=True, max_retries=3)
def analyze_sentiment(self, previous_result: dict) -> dict:
    """Stage 5 — Analyze sentiment (-1.0 to +1.0) using VADER. <-0.6 = critical."""
    if "error" in previous_result:
        return previous_result
    return run_in_celery(
        _analyze_sentiment_async(previous_result["feedback_item_id"], previous_result["tenant_id"])
    )
