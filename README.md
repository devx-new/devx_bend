# Developer Feedback Platform (DFP)

The Developer Feedback Platform (DFP) is a comprehensive system designed to collect, process, and act upon developer feedback. It utilizes an AI-driven pipeline to normalize, classify, analyze sentiment, prioritize, and route feedback to appropriate channels.

## Architecture & Tech Stack

**Backend:**
*   **Framework:** FastAPI (async)
*   **Task Queue:** Celery
*   **Cache/Broker:** Redis
*   **Database:** SQLite (dev) / PostgreSQL + pgvector (prod)
*   **Migrations:** Alembic
*   **Auth:** OAuth2 + JWT (Secure HttpOnly cookies), bcrypt API keys

**AI Pipeline:**
*   **Embeddings:** `sentence-transformers/all-MiniLM-L6-v2` (384-dim)
*   **Classification:** `distilBERT` (bug, feature, docs, perf, security)
*   **Sentiment Analysis:** `VADER` (with `distilBERT-SST2` fallback)
*   **Prioritization:** `XGBoost`
*   **Summarization:** Gemini API (weekly digests)

**Frontend (Planned/In Development):**
*   **Framework:** React 18 + Vite
*   **Language:** TypeScript
*   **Styling:** Tailwind CSS + shadcn/ui
*   **State Management:** Zustand + TanStack Query

## AI Pipeline Stages

The platform features an 8-stage asynchronous pipeline orchestrated via Celery:
1.  **Normalize**: Standardize incoming feedback text.
2.  **Embed**: Generate semantic embeddings using `all-MiniLM-L6-v2`.
3.  **Semantic Dedup**: Detect duplicates using cosine similarity (>0.88).
4.  **Classify**: Categorize feedback using `distilBERT`.
5.  **Sentiment**: Analyze sentiment (-1.0 to +1.0).
6.  **Priority Score**: Generate priority 0-100 via `XGBoost`.
7.  **Route**: Dispatch to integrations (Slack/Jira/Linear) based on configurable rules.
8.  **Digest**: Generate weekly summaries via Gemini API.

## Prerequisites

*   Python 3.10+
*   Docker & Docker Compose (for Redis and PostgreSQL)
*   Node.js 18+ (for frontend development)

## Getting Started

### 1. Environment Setup

Create a `.env` file in the root directory and configure the necessary environment variables based on `app/config.py`.

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Infrastructure Services

Start Redis and PostgreSQL (with pgvector) using Docker Compose:

```bash
docker-compose up -d
```

### 3. Database Migrations

Apply the latest Alembic migrations to set up your database schema:

```bash
alembic upgrade head
```

### 4. Running the Application

Start the FastAPI development server:

```bash
uvicorn app.main:app --reload
```
The API will be available at `http://localhost:8000`.

Start the Celery worker for processing asynchronous tasks:

```bash
celery -A app.worker worker -l info
```

Start the Celery beat process for scheduled tasks (e.g., weekly digests):

```bash
celery -A app.worker beat -l info
```

### 5. Frontend Development

To run the frontend:
```bash
npm install
npm run dev
```

## Integration

### API Contract

All endpoints follow a strict contract enforcing tenant isolation, authentication, authorization, validation, audit logging, and rate limiting.

**Success Response:**
```json
{ 
  "success": true, 
  "data": {}, 
  "meta": {} 
}
```

**Error Response:**
```json
{ 
  "success": false, 
  "error": { 
    "code": "ERROR_CODE", 
    "message": "Error description" 
  } 
}
```

### Constraints & Security
*   **Payload Size:** Max 64KB per request.
*   **Auth Transport:** Secure, HttpOnly cookies for JWT with double-submit CSRF protection.
*   **File Uploads:** Max 25MB (handled via Cloudinary signed URLs).
*   **Rate Limits:** Enforced via Redis sliding window (e.g., 100/min/tenant for notifications).
*   **Audit Logging:** Append-only logging of all mutations.

## Migration Guide (Database)

We use Alembic for database migrations. To create a new migration after updating your SQLAlchemy models:

1.  Generate a new revision:
    ```bash
    alembic revision --autogenerate -m "description of your changes"
    ```
2.  Review the generated script in `migrations/versions/`.
3.  Apply the migration:
    ```bash
    alembic upgrade head
    ```

To downgrade to a previous migration:
```bash
alembic downgrade -1
```
