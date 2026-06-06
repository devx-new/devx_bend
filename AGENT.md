# Developer Feedback Platform (DFP)

# Backend Agent Ruleset & API Governance Specification

**Version:** 1.0 MVP
**Scope:** FastAPI + Celery + AI Processing Pipeline
**Architecture Style:** Agent-Oriented Backend Services

---

# 1. Purpose

The backend is not a collection of endpoints.

It is a collection of specialized agents responsible for:

* Data ingestion
* AI enrichment
* Routing
* Analytics
* Security
* Observability

Each agent has:

```text
Responsibilities
Inputs
Outputs
Constraints
Permissions
Failure Handling
```

Agents communicate through:

```text
FastAPI Requests
Database Events
Celery Tasks
Redis Pub/Sub
```

---

# 2. Backend Agent Hierarchy

```text
System Orchestrator
│
├── Auth Agent
├── Tenant Agent
├── Ingestion Agent
├── Deduplication Agent
├── Classification Agent
├── Sentiment Agent
├── Priority Agent
├── Routing Agent
├── Analytics Agent
├── Digest Agent
├── Notification Agent
├── Survey Agent
├── Audit Agent
├── Integration Agent
├── File Agent
└── Security Agent
```

---

# 3. Global Rules (Applies To Every Agent)

## Rule G-001

Every operation must be tenant-scoped.

```python
tenant_id REQUIRED
```

Never process unscoped data.

---

## Rule G-002

Agents never trust client data.

Always validate via:

```python
Pydantic
```

before processing.

---

## Rule G-003

Agents never directly call other agents.

Communication happens through:

```python
Celery Tasks
Database State
Redis Events
```

---

## Rule G-004

Agents must be idempotent.

The same request:

```text
1 time
10 times
100 times
```

must produce the same outcome.

---

## Rule G-005

All mutations generate audit events.

```python
AuditLog
```

must be written.

No exceptions.

---

## Rule G-006

Failures must never crash workers.

Return:

```python
Retry
Dead Letter
Error State
```

---

# 4. Auth Agent

## Responsibility

Authentication and authorization.

---

## Allowed Actions

```text
Login
Refresh Tokens
Validate JWT
Validate API Keys
OAuth Exchange
RBAC Check
```

---

## Forbidden Actions

```text
Business Logic
Feedback Processing
AI Processing
```

---

## Rules

### AUTH-001

JWT expiration maximum:

```text
15 minutes
```

---

### AUTH-002

Refresh token maximum:

```text
7 days
```

---

### AUTH-003

Passwords:

```python
bcrypt
```

minimum cost factor:

```text
12
```

---

### AUTH-004

Failed login attempts:

```text
5 attempts
```

lock account for:

```text
15 minutes
```

---

# 5. Tenant Agent

## Responsibility

Tenant isolation.

---

## Rules

### TENANT-001

Every query must contain:

```python
tenant_id
```

---

### TENANT-002

Cross-tenant reads prohibited.

---

### TENANT-003

Cross-tenant writes prohibited.

---

### TENANT-004

Global admin bypass disabled in MVP.

---

# 6. Ingestion Agent

## Responsibility

Receive incoming data.

Sources:

```text
GitHub
GitLab
Slack
Discord
Jira
Linear
Portal
```

---

## Rules

### INGEST-001

Verify webhook signatures first.

Before:

```python
JSON parsing
DB writes
```

---

### INGEST-002

Payload size:

```text
64KB max
```

---

### INGEST-003

Normalize data.

Required fields:

```python
source
external_id
title
body
author
created_at
tenant_id
```

---

### INGEST-004

Store raw payload.

```python
audit_log
```

reference only.

Never expose externally.

---

### INGEST-005

Queue AI processing.

Never run AI synchronously.

---

# 7. Deduplication Agent

## Responsibility

Prevent duplicate issues.

---

## Rules

### DEDUP-001

Hash:

```python
tenant_id + external_id
```

---

### DEDUP-002

Similarity threshold:

```text
0.88
```

---

### DEDUP-003

Duplicate groups must have:

```python
canonical_item_id
```

---

### DEDUP-004

Duplicates inherit routing history.

---

# 8. Classification Agent

## Responsibility

Categorize feedback.

---

## Allowed Categories

```text
bug
feature
docs
performance
security
```

---

## Rules

### CLASS-001

Confidence required.

```python
confidence_score
```

---

### CLASS-002

Confidence below:

```text
0.60
```

becomes:

```text
uncategorized
```

---

### CLASS-003

Store model version.

Always.

---

# 9. Sentiment Agent

## Responsibility

Evaluate developer sentiment.

---

## Rules

### SENT-001

Range:

```text
-1.0 → +1.0
```

---

### SENT-002

Below:

```text
-0.60
```

mark:

```text
critical sentiment
```

---

### SENT-003

Never route solely on sentiment.

Sentiment is advisory.

---

# 10. Priority Agent

## Responsibility

Score urgency.

---

## Formula Inputs

```text
Frequency
Sentiment
Recency
Category
Author Reputation
```

---

## Rules

### PRIORITY-001

Score range:

```text
0 → 100
```

---

### PRIORITY-002

Above:

```text
80
```

Critical.

---

### PRIORITY-003

Above:

```text
60
```

High.

---

### PRIORITY-004

Store scoring explanation.

Example:

```json
{
  "frequency": 0.92,
  "recency": 0.75,
  "sentiment": -0.84
}
```

---

# 11. Routing Agent

## Responsibility

Execute automations.

---

## Actions

```text
Slack Notify
Jira Create
Linear Create
Email
```

---

## Rules

### ROUTE-001

Rules evaluated in priority order.

---

### ROUTE-002

Rules must be deterministic.

---

### ROUTE-003

Same rule may only fire once.

Store:

```python
notifications_log
```

---

### ROUTE-004

Failures retried:

```text
3 times
```

---

# 12. Notification Agent

## Responsibility

Outbound communication.

---

## Rules

### NOTIFY-001

Never block API requests.

Async only.

---

### NOTIFY-002

Every delivery logged.

---

### NOTIFY-003

Payloads redact secrets.

---

### NOTIFY-004

Rate limit:

```text
100 notifications/minute/tenant
```

---

# 13. Analytics Agent

## Responsibility

Generate KPIs.

---

## Rules

### ANALYTICS-001

Read-only access.

---

### ANALYTICS-002

No external API calls.

---

### ANALYTICS-003

Cache expensive queries.

```python
Redis
```

TTL:

```text
5 minutes
```

---

### ANALYTICS-004

Aggregate only.

Never expose raw tenant data.

---

# 14. Digest Agent

## Responsibility

Weekly summaries.

Powered by Gemini.

---

## Rules

### DIGEST-001

Run weekly.

---

### DIGEST-002

Maximum issues analyzed:

```text
Top 100
```

---

### DIGEST-003

Remove PII before prompt generation.

---

### DIGEST-004

Prompt hash cache:

```text
1 hour
```

---

### DIGEST-005

Store generated digest.

Never regenerate identical content.

---

# 15. Survey Agent

## Responsibility

Developer Experience metrics.

---

## Rules

### SURVEY-001

Allowed range:

```text
1–10
```

---

### SURVEY-002

One response per user per cycle.

---

### SURVEY-003

Anonymous surveys permitted.

---

### SURVEY-004

Responses immutable.

---

# 16. File Agent

## Responsibility

Attachment handling.

---

## Rules

### FILE-001

Allowed types

```text
png
jpg
jpeg
pdf
txt
md
```

---

### FILE-002

Maximum file size

```text
25MB
```

---

### FILE-003

Virus scan before persistence.

---

### FILE-004

Store only Cloudinary URLs.

Never binary blobs in DB.

---

# 17. Audit Agent

## Responsibility

Compliance trail.

---

## Rules

### AUDIT-001

Every mutation logged.

---

### AUDIT-002

Append-only.

No updates.

No deletes.

---

### AUDIT-003

Capture:

```json
{
  "actor",
  "resource",
  "action",
  "before",
  "after",
  "timestamp"
}
```

---

### AUDIT-004

Retention:

```text
7 years
```

---

# 18. Security Agent

## Responsibility

Platform defense.

---

## Rules

### SEC-001

All inputs sanitized.

---

### SEC-002

HTML stripped.

---

### SEC-003

SQLAlchemy ORM only.

No dynamic SQL.

---

### SEC-004

Secrets never logged.

---

### SEC-005

Webhook verification mandatory.

---

### SEC-006

Per-tenant rate limiting.

---

### SEC-007

PII scrubber executes before LLM usage.

---

### SEC-008

Failed verification events trigger audit records.

---

# 19. System Orchestrator Rules

The System Orchestrator coordinates all backend agents.

### ORCH-001

No business logic.

---

### ORCH-002

No AI logic.

---

### ORCH-003

Only responsibilities:

```text
Workflow Coordination
Task Scheduling
Health Monitoring
Retry Management
```

---

### ORCH-004

Maximum workflow runtime:

```text
2 minutes
```

---

### ORCH-005

All long-running work delegated to Celery.

---

# 20. API Contract Enforcement Rules

Every endpoint must satisfy:

```text
Authentication
Authorization
Validation
Audit Logging
Tenant Isolation
Rate Limiting
Error Standardization
```

### Standard Success Response

```json
{
  "success": true,
  "data": {},
  "meta": {}
}
```

### Standard Error Response

```json
{
  "success": false,
  "error": {
    "code": "INVALID_PAYLOAD",
    "message": "Validation failed"
  }
}
```

---

# Backend Golden Rule

Every backend agent must answer **one question only**:

> "Given this piece of developer feedback, what is the safest, most accurate, and most useful action I can take within my area of responsibility?"

Any logic outside that responsibility belongs to another agent. This keeps the FastAPI monolith modular, testable, and ready for future migration into microservices or autonomous AI-assisted workflows without major rewrites.
