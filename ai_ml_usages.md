# AI / ML Usages

Overview of every AI and ML component in the DevX Backend pipeline, including the model, provider, purpose, fallback, and where the code lives.

---

## 1. Feedback Classification

| | |
|---|---|
| **Purpose** | Categorise each feedback item into one of: `bug`, `feature`, `docs`, `performance`, `security` |
| **Primary model** | `meta/llama-3.1-8b-instruct` via **NVIDIA NIM** (free tier) |
| **Fallback model** | `gemini-2.5-flash` via **Google Gemini API** |
| **Input** | `item.body` or `item.title` |
| **Output** | `FeedbackItem.category` + a `FeedbackTag` row (category, confidence, model version) |
| **Trigger** | Celery task `classify_feedback` — Stage 4 of the ingestion pipeline |
| **Code** | `app/agents/ai_processing.py` → `_classify_feedback_async` |
| **Env vars** | `NVIDIA_API_KEY`, `gemini_api_key` |

**Prompt strategy:** zero-shot instruction to output only the category name in lowercase. Temperature 0.1 for determinism. Falls back to Gemini if NVIDIA returns an error or an unrecognised label.

---

## 2. Priority Scoring

| | |
|---|---|
| **Purpose** | Assign a 0–100 urgency/impact score (>80 = critical, >60 = high, 40–60 = medium, <40 = low) |
| **Primary model** | `meta/llama-3.1-8b-instruct` via **NVIDIA NIM** |
| **Fallback model** | `gemini-2.5-flash` via **Google Gemini API** |
| **Input** | `item.title`, `item.body`, `item.sentiment_score`, `item.category` |
| **Output** | `FeedbackItem.priority_score` (float 0–100) |
| **Trigger** | Celery task `calculate_priority` — Stage 6 of the pipeline |
| **Code** | `app/agents/prioritization.py` → `_calculate_priority_async` |
| **Env vars** | `NVIDIA_API_KEY`, `gemini_api_key` |

**Prompt strategy:** triage assistant persona, asks for a single integer 0–100. Numeric parsing strips non-digit characters to handle responses like "75/100" or "75.". Temperature 0.1.

---

## 3. Sentiment Analysis

| | |
|---|---|
| **Purpose** | Score each item on a −1.0 (very negative) to +1.0 (very positive) scale. Scores below −0.6 are flagged as critical. |
| **Model** | **VADER** (`vaderSentiment`) — local, no API call |
| **Input** | `item.body` or `item.title` |
| **Output** | `FeedbackItem.sentiment_score` (float) |
| **Trigger** | Celery task `analyze_sentiment` — Stage 5 of the pipeline |
| **Code** | `app/agents/ai_processing.py` → `_analyze_sentiment_async` |
| **Env vars** | None — fully local |

**Why VADER:** fast, runs in-process, no network dependency, and well-calibrated for short developer text.

---

## 4. Semantic Embeddings

| | |
|---|---|
| **Purpose** | Produce a 384-dimensional vector per item, used by the deduplication stage for cosine-similarity matching |
| **Model** | `sentence-transformers/all-MiniLM-L6-v2` via **HuggingFace Inference API** |
| **Input** | `item.body` or `item.title` |
| **Output** | `FeedbackItem.embedding` (vector stored in pgvector) |
| **Trigger** | Celery task `embed_feedback` — Stage 2 of the pipeline |
| **Code** | `app/agents/ai_processing.py` → `_embed_feedback_async` |
| **Env vars** | `huggingface_api_key` |
| **Failure mode** | Non-fatal — if the HuggingFace API is unreachable the item continues through the pipeline without an embedding; deduplication is skipped for that item |

---

## 5. Semantic Deduplication

| | |
|---|---|
| **Purpose** | Detect near-duplicate feedback items so the same issue isn't processed and routed multiple times |
| **Technique** | pgvector cosine distance search — items within distance < 0.12 (cosine similarity > 0.88) are considered duplicates |
| **Input** | `FeedbackItem.embedding` (from Stage 2) |
| **Output** | Marks item as `status = "duplicate"`, creates a `DuplicateGroup` record linking it to the canonical item |
| **Trigger** | Celery task `dedup_feedback` — Stage 3 of the pipeline |
| **Code** | `app/agents/deduplication.py` → `_dedup_feedback_async` |
| **Env vars** | None (uses local pgvector) |
| **Failure mode** | Skipped gracefully if no embedding is available |

---

## 6. Weekly Digest Generation

| | |
|---|---|
| **Purpose** | Produce a Markdown executive summary of the week's feedback per tenant: top issues, feature requests, sentiment overview, and recommended actions |
| **Model** | `gemini-2.5-flash` via **Google Gemini API** |
| **Input** | Top 100 feedback items from the past 7 days (title, category, priority, sentiment), PII-stripped before sending |
| **Output** | `WeeklyDigest.report_markdown` stored in the database and served via the analytics API |
| **Trigger** | Celery beat task `generate_weekly_digest` (scheduled) or on-demand via `POST /v1/analytics/digests/generate` |
| **Code** | `app/agents/digest.py` → `_generate_weekly_digest_async`, `_generate_digest_for_tenant_async` |
| **Env vars** | `gemini_api_key` |
| **PII handling** | Emails and phone numbers are redacted to `[REDACTED]` before the prompt is sent |

---

## Pipeline Flow Summary

```
Inbound feedback (Slack / GitHub / Jira webhook / API)
        │
        ▼
[1] normalize_feedback     — regex cleaning, HTML strip, PII prefix strip
        │
        ▼
[2] embed_feedback         — HuggingFace all-MiniLM-L6-v2  (384-dim vector)
        │
        ▼
[3] dedup_feedback         — pgvector cosine similarity (threshold 0.88)
        │
        ▼
[4] classify_feedback      — NVIDIA NIM Llama-3.1-8B → Gemini fallback
        │
        ▼
[5] analyze_sentiment      — VADER (local, no API)
        │
        ▼
[6] calculate_priority     — NVIDIA NIM Llama-3.1-8B → Gemini fallback
        │
        ▼
[7] route_feedback         — Jira ticket creation (outbound)
```

---

## Environment Variables Reference

| Variable | Used by |
|---|---|
| `NVIDIA_API_KEY` | Classification (primary), Priority scoring (primary) |
| `gemini_api_key` | Classification (fallback), Priority scoring (fallback), Weekly digest |
| `huggingface_api_key` | Semantic embeddings |
| `DIGEST_PROVIDER` | Selects digest model: `"nvidia"` or `"gemini"` (default `"nvidia"`) |
