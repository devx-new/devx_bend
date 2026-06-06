.PHONY: help install dev worker beat migrate migrate-create db-up db-down logs lint test seed

# Default target
help:
	@echo ""
	@echo "  Developer Feedback Platform — Available Commands"
	@echo "  ================================================="
	@echo ""
	@echo "  Setup"
	@echo "    make install          Install Python dependencies"
	@echo "    make db-up            Start PostgreSQL + Redis via Docker Compose"
	@echo "    make db-down          Stop Docker Compose services"
	@echo ""
	@echo "  Database"
	@echo "    make migrate          Apply all pending Alembic migrations"
	@echo "    make migrate-create m=\"description\"  Create a new migration"
	@echo "    make migrate-down     Rollback the last migration"
	@echo ""
	@echo "  Run"
	@echo "    make dev              Start FastAPI dev server (port 8000)"
	@echo "    make worker           Start Celery worker"
	@echo "    make beat             Start Celery beat scheduler (weekly digest)"
	@echo ""
	@echo "  Dev Tools"
	@echo "    make logs             Tail the rotating log file"
	@echo "    make lint             Run ruff linter"
	@echo "    make test             Run pytest"
	@echo "    make seed             Seed the database with test data"
	@echo ""

# ─── Setup ─────────────────────────────────────────────────────────────────────

install:
	pip install -r requirements.txt

db-up:
	docker compose up -d

db-down:
	docker compose down

# ─── Database ──────────────────────────────────────────────────────────────────

migrate:
	alembic upgrade head

migrate-create:
	@if [ -z "$(m)" ]; then echo "Usage: make migrate-create m=\"your description\""; exit 1; fi
	alembic revision --autogenerate -m "$(m)"

migrate-down:
	alembic downgrade -1

# ─── Run ───────────────────────────────────────────────────────────────────────

dev:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:
	celery -A app.worker.celery_app worker --loglevel=info

beat:
	celery -A app.worker.celery_app beat --loglevel=info

# ─── Dev Tools ─────────────────────────────────────────────────────────────────

logs:
	tail -f logs/dfp.log

lint:
	ruff check app/

test:
	PYTHONPATH=. pytest -v

seed:
	PYTHONPATH=. python scripts/seed.py
