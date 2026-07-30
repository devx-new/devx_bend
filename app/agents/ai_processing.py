import logging

from celery import shared_task
from openai import OpenAI
from sqlalchemy import select
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from app.config import settings
from app.database import async_session_factory, run_in_celery
from app.models.audit import AuditLog
from app.models.feedback import FeedbackItem, FeedbackTag

logger = logging.getLogger(__name__)

# Loaded once per worker process; subsequent tasks reuse the in-memory model
_embed_model = None

def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2", local_files_only=True)
        logger.info("SentenceTransformer model loaded into worker")
    return _embed_model


NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_CLASSIFY_MODEL = "meta/llama-3.1-8b-instruct"  # lightweight for classify

CATEGORIES = [
    "bug", "feature", "docs", "performance", "security",
    "praise", "ux", "billing", "onboarding", "support",
    "accessibility", "integration", "churn-risk",
]

_CATEGORY_HINTS = (
    "bug: something broken or not working as expected\n"
    "feature: a request for new functionality\n"
    "docs: documentation is missing, wrong, or confusing\n"
    "performance: something is slow or resource-heavy\n"
    "security: a vulnerability or security concern\n"
    "praise: a compliment or positive reaction, with no problem to fix\n"
    "ux: confusing or hard to use, but not technically broken\n"
    "billing: pricing, invoicing, subscription, or payment issues\n"
    "onboarding: friction during first-time setup or getting started\n"
    "support: account access, login, or other account-specific trouble\n"
    "accessibility: a11y barriers — screen readers, keyboard nav, contrast, etc.\n"
    "integration: something wrong with a specific connected third-party tool, not the core product\n"
    "churn-risk: signals the customer may cancel or is seriously dissatisfied, beyond a normal complaint\n"
)


def _get_nvidia_client() -> OpenAI:
    return OpenAI(
        base_url=NVIDIA_BASE_URL,
        api_key=settings.nvidia_api_key,
        timeout=30.0,
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
        if not item:
            return {"error": "Feedback not found or empty"}
        text_to_embed = " ".join(filter(None, [item.title, item.body]))
        if not text_to_embed:
            return {"error": "Feedback not found or empty"}

        try:
            model = _get_embed_model()
            vector = model.encode(text_to_embed, normalize_embeddings=True).tolist()
            if len(vector) == 384:
                item.embedding = vector
                logger.info("Generated embedding size=384 for item=%s (local)", item.id)
            else:
                logger.warning("Unexpected embedding size=%d for item=%s", len(vector), item.id)
        except Exception as exc:
            logger.warning("Embedding skipped for item=%s: %s", item.id, exc)

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

        feedback_text = " ".join(filter(None, [item.title, item.body]))
        prompt = (
            "Classify the following developer feedback into exactly ONE of these categories:\n"
            f"{_CATEGORY_HINTS}\n"
            f"Feedback: {feedback_text}\n\n"
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
                logger.warning(
                    "NVIDIA classify failed for item=%s: %s — falling back", item.id, exc
                )

        # Fallback: Gemini
        if model_used == "fallback" and settings.gemini_api_key:
            try:
                from google import genai
                from google.genai import types as gtypes

                g_client = genai.Client(api_key=settings.gemini_api_key)
                g_response = g_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(temperature=0.1, http_options=gtypes.HttpOptions(timeout=30000)),
                )
                raw = g_response.text.strip().lower()
                category = raw if raw in CATEGORIES else "uncategorized"
                confidence = 0.85
                model_used = "gemini-2.5-flash"
                logger.info(
                    "Classified item=%s as '%s' via Gemini fallback", item.id, category
                )
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
        return {
            "feedback_item_id": feedback_item_id,
            "tenant_id": tenant_id,
            "status": "classified",
        }


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
        if not item:
            return {"error": "Feedback not found or empty"}
        text_for_sentiment = " ".join(filter(None, [item.title, item.body]))
        if not text_for_sentiment:
            return {"error": "Feedback not found or empty"}

        sentiment = None

        # Primary: NVIDIA NIM — handles any language
        if settings.nvidia_api_key:
            try:
                client = _get_nvidia_client()
                prompt = (
                    "Analyze the sentiment of the following developer feedback. "
                    "Reply with ONLY a single decimal number between -1.0 (very negative) "
                    "and 1.0 (very positive), for example: -0.72 or 0.45\n\n"
                    f"Feedback: {text_for_sentiment}"
                )
                resp = client.chat.completions.create(
                    model=NVIDIA_CLASSIFY_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=8,
                )
                raw = resp.choices[0].message.content.strip()
                numeric = "".join(c for c in raw if c.isdigit() or c in ".-")
                score = float(numeric)
                if -1.0 <= score <= 1.0:
                    sentiment = round(score, 4)
                    logger.info("Sentiment %.4f for item=%s via NVIDIA NIM", sentiment, item.id)
            except Exception as exc:
                logger.warning("NVIDIA sentiment failed for item=%s: %s — falling back", item.id, exc)

        # Fallback: VADER (English only)
        if sentiment is None:
            analyzer = SentimentIntensityAnalyzer()
            vs = analyzer.polarity_scores(text_for_sentiment)
            sentiment = round(vs["compound"], 4)
            logger.info("Sentiment %.4f for item=%s via VADER", sentiment, item.id)

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
