import logging
from pathlib import Path
from typing import Any

import resend
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings

logger = logging.getLogger(__name__)

resend.api_key = settings.resend_api_key

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "emails"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)


def render_email(template: str, **context: Any) -> str:
    """Render an email template from app/templates/emails/ (e.g. "generic.html")."""
    return _env.get_template(template).render(**context)


async def send_email(
    to: str | list[str],
    subject: str,
    html: str,
    text: str | None = None,
) -> str | None:
    """Send an email via Resend. Returns the Resend message id, or None if sending failed."""
    if not settings.resend_api_key:
        logger.warning("resend_api_key not configured — skipping email send", extra={"subject": subject})
        return None

    params: resend.Emails.SendParams = {
        "from": settings.email_from,
        "to": to,
        "subject": subject,
        "html": html,
    }
    if text:
        params["text"] = text

    try:
        response = await resend.Emails.send_async(params)
        return response.get("id") if isinstance(response, dict) else getattr(response, "id", None)
    except Exception as exc:
        logger.error("Failed to send email via Resend", extra={"subject": subject, "error": str(exc)})
        return None


async def send_templated_email(
    to: str | list[str],
    subject: str,
    template: str = "generic.html",
    **context: Any,
) -> str | None:
    """Render `template` from app/templates/emails/ and send it via Resend."""
    html = render_email(template, subject=subject, **context)
    return await send_email(to, subject, html)
