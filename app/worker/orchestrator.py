from celery import chain
from app.worker.celery_app import celery_app

@celery_app.task(bind=True, max_retries=3)
def start_feedback_pipeline(self, feedback_item_id: str, tenant_id: str):
    """
    The System Orchestrator.
    Kicks off the 8-stage AI processing pipeline for a given piece of feedback.
    Stages:
      1. Normalize
      2. Embed
      3. Semantic Dedup
      4. Classify
      5. Sentiment
      6. Priority Score
      7. Route
    (Digest is a separate scheduled task).
    """
    
    # We pass the feedback_item_id through the chain.
    # Each task updates the DB and returns the ID for the next task.
    pipeline = chain(
        celery_app.signature("app.agents.ingestion.normalize_feedback", args=[feedback_item_id, tenant_id]),
        celery_app.signature("app.agents.ai_processing.embed_feedback"),
        celery_app.signature("app.agents.deduplication.dedup_feedback"),
        celery_app.signature("app.agents.ai_processing.classify_feedback"),
        celery_app.signature("app.agents.ai_processing.analyze_sentiment"),
        celery_app.signature("app.agents.prioritization.calculate_priority"),
        celery_app.signature("app.agents.routing.route_feedback")
    )
    
    pipeline.apply_async()
    return {"status": "pipeline_started", "feedback_item_id": feedback_item_id}
