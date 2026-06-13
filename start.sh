#!/bin/bash
set -e

case "$SERVICE_TYPE" in
  worker)
    exec celery -A app.worker.celery_app worker --loglevel=info --concurrency=2
    ;;
  beat)
    exec celery -A app.worker.celery_app beat --loglevel=info --schedule=/tmp/celerybeat-schedule
    ;;
  *)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port $PORT
    ;;
esac
