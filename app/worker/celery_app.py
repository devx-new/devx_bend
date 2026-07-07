from celery import Celery
from app.config import settings

celery_app = Celery(
    "dfp_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.agents.ingestion",
        "app.agents.ai_processing",
        "app.agents.deduplication",
        "app.agents.prioritization",
        "app.agents.routing",
        "app.agents.digest",
        "app.worker.orchestrator",
        "app.worker.email_tasks",
    ]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=120, # Maximum workflow runtime: 2 minutes
    worker_max_tasks_per_child=1000,
)

# Optional: define beat schedule for the Digest Agent
celery_app.conf.beat_schedule = {
    "weekly-digest": {
        "task": "app.agents.digest.generate_weekly_digest",
        "schedule": 604800.0, # Every 7 days
    },
}
