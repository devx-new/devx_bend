import logging

from app.core.email import send_templated_email
from app.database import run_in_celery
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3)
def send_templated_email_task(
    self,
    to: str | list[str],
    subject: str,
    template: str = "generic.html",
    **context,
) -> dict:
    """
    Background job for all outbound email. Renders `template` from
    app/templates/emails/ and sends it via Resend. Call this instead of
    app.core.email.send_templated_email directly so email delivery never
    blocks a request/webhook response.
    """
    message_id = run_in_celery(send_templated_email(to, subject, template, **context))
    return {"status": "sent" if message_id else "failed", "message_id": message_id}
