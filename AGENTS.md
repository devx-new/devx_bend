# DFP — Developer Feedback Platform

## Project State
Greenfield — no source code yet. Design sources: `AGENT.md`, `DFP_System_Requirements_Plan_v2.docx`.

## Stack
**Backend:** FastAPI async + Celery + Redis + SQLite (dev) / PostgreSQL+pgvector (prod) + Alembic
**Frontend:** React 18 + TypeScript + Vite + shadcn/ui + Zustand + TanStack Query + Recharts
**AI:** sentence-transformers/all-MiniLM-L6-v2, distilBERT, VADER, XGBoost, Gemini API
**Auth:** OAuth2 + JWT (15min access / 7d refresh), HMAC-SHA256 webhooks, bcrypt API keys (cost ≥12)
**Files:** Cloudinary (signed URLs, no binary blobs in DB)

## Agent Architecture
16 agents (hierarchy in `AGENT.md`). Key rules:
- Every operation tenant-scoped (`tenant_id` on all tables, global SQLAlchemy filter)
- Agents never call each other directly — communicate via Celery tasks, DB state, Redis events
- All mutations → `audit_log` (append-only, 7yr retention, no updates/deletes)
- All operations idempotent; failures → retry/dead-letter, never crash workers
- Pydantic strict validation on all inputs; max payload 64KB
- No business logic or AI logic in System Orchestrator (coordination only, max 2min runtime)

## Dev Commands
```sh
uvicorn app.main:app --reload          # FastAPI on :8000
celery -A app.worker worker -l info    # Celery worker
celery -A app.worker beat -l info      # Celery beat (weekly digest)
docker-compose up                      # Redis + PostgreSQL+pgvector
alembic upgrade head                   # DB migrations
npm run dev                            # Frontend on :5173
pytest                                 # Tests (SQLite in-memory, no deps)
```

## AI Pipeline (8 stages, async Celery chain)
1. Normalize → 2. Embed (all-MiniLM-L6-v2, 384-dim) → 3. Semantic Dedup (pgvector cosine >0.88)
4. Classify (distilBERT: bug/feature/docs/perf/security; <0.60→uncategorized)
5. Sentiment (VADER → distilBERT-SST2 fallback; range -1.0 to +1.0, <-0.6=critical)
6. Priority Score (XGBoost: 0-100, >80 critical, >60 high)
7. Route (configurable rules → Slack/Jira/Linear; 3 retries, fire-once per rule)
8. Digest (Gemini, weekly, top 100 issues, PII stripped, prompt hash cached 1h in Redis)

Model versions logged in MLflow (SQLite backend). Embedding re-indexing: batches of 200.

## Logging & Monitoring
- **Stdout:** structlog JSON lines (primary for `docker-compose logs`)
- **Rotating file:** `logs/dfp.log` — `RotatingFileHandler`, 100MB per file, 5 backups, JSON lines
- **Audit DB:** `audit_log` table — append-only, all mutations captured with actor/resource/action/diff/timestamp
- **Delivery tracking:** `notifications_log` table for every outbound notification
- **Config:** controlled via `.env` — `LOG_FILE`, `LOG_LEVEL`, `LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`
- All services (FastAPI, Celery worker, Celery beat) write to the same file
- Per-tenant Redis sliding window rate limits

## API Contract
Every endpoint: auth → authZ → validation → audit logging → tenant isolation → rate limiting → standard error response.
```json
// Success
{ "success": true, "data": {}, "meta": {} }
// Error
{ "success": false, "error": { "code": "INVALID_PAYLOAD", "message": "..." } }
```

## Key Constraints
| Item | Value |
|------|-------|
| Payload max | 64KB |
| File upload max | 25MB (Cloudinary only) |
| JWT access | 15 min |
| JWT refresh | 7 days |
| bcrypt cost | ≥12 |
| Login lockout | 5 attempts → 15 min |
| Notif rate limit | 100/min/tenant |
| Dedup threshold | 0.88 cosine |
| Sentiment range | -1.0 to +1.0 |
| Priority range | 0–100 |
| Max workflow runtime | 2 min |
| Digest prompts cached | 1h TTL |
| Analytics cache | 5min TTL (Redis) |
