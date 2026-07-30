import logging
import re

from app.agents.ai_processing import NVIDIA_CLASSIFY_MODEL, _get_nvidia_client
from app.config import settings

logger = logging.getLogger(__name__)

_NOISE_SENDER_PATTERN = re.compile(
    r"(no-?reply|do-?not-?reply|mailer-daemon|notifications?|newsletter|marketing|alerts?|"
    r"digest|bounce|postmaster)@",
    re.IGNORECASE,
)

_NOISE_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "CATEGORY_FORUMS", "SPAM", "TRASH"}

_MIN_BODY_LENGTH = 5


def is_noise_by_rules(message: dict) -> bool:
    """Cheap deterministic pre-filter. True if the message is obviously not feedback,
    so it never reaches the (paid, slower) LLM relevance check.
    """
    sender = (message.get("from") or "").lower()
    if _NOISE_SENDER_PATTERN.search(sender):
        return True

    labels = set(message.get("label_ids") or [])
    if labels & _NOISE_LABELS:
        return True

    body = (message.get("body") or "").strip()
    if len(body) < _MIN_BODY_LENGTH:
        return True

    return False


async def is_feedback_relevant(subject: str, body: str, sender: str) -> bool:
    """LLM check: is this email genuine product feedback, or unrelated correspondence/noise?

    Fails open (returns True) when no LLM backend is reachable, so an API outage
    never silently drops real feedback.
    """
    prompt = (
        "You are filtering an inbox/channel that receives both genuine product feedback and unrelated "
        "traffic. Decide whether the message below is genuine user/customer feedback about a product or "
        "service — this includes bug reports, feature requests, complaints, support issues, AND short "
        "praise or reactions about the product (e.g. \"Nice UI\", \"love the new dashboard\", \"this is "
        "slow\"). Brevity alone does NOT make something non-feedback — a two-word compliment or gripe "
        "about the product still counts as feedback.\n\n"
        "It is NOT feedback if it is personal correspondence, an unrelated business email, a newsletter, "
        "marketing, a receipt/invoice, a calendar invite/meeting note, an automated notification, or spam.\n\n"
        "Examples:\n"
        "\"Nice UI look\" -> yes\n"
        "\"this is way too slow\" -> yes\n"
        "\"can you add dark mode?\" -> yes\n"
        "\"Meeting confirmed for 3pm tomorrow\" -> no\n"
        "\"50% off your next order, today only!\" -> no\n\n"
        f"From: {sender}\n"
        f"Subject: {subject}\n"
        f"Body: {body[:2000]}\n\n"
        "Reply with ONLY 'yes' or 'no'."
    )

    if settings.nvidia_api_key:
        try:
            client = _get_nvidia_client()
            response = client.chat.completions.create(
                model=NVIDIA_CLASSIFY_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=4,
            )
            raw = response.choices[0].message.content.strip().lower()
            if raw.startswith("yes"):
                return True
            if raw.startswith("no"):
                return False
        except Exception as exc:
            logger.warning("NVIDIA relevance check failed: %s — falling back", exc)

    if settings.gemini_api_key:
        try:
            from google import genai
            from google.genai import types as gtypes

            g_client = genai.Client(api_key=settings.gemini_api_key)
            g_response = g_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=gtypes.GenerateContentConfig(temperature=0.0, http_options=gtypes.HttpOptions(timeout=30000)),
            )
            raw = g_response.text.strip().lower()
            if raw.startswith("yes"):
                return True
            if raw.startswith("no"):
                return False
        except Exception as exc:
            logger.error("Gemini relevance check fallback also failed: %s", exc)

    logger.warning("Relevance check unavailable (no working LLM backend) — defaulting to relevant")
    return True
