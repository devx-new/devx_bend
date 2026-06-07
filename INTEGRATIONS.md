# Integrations

This document covers how the DevFeedback integration system works end-to-end: data flow, the AI pipeline, each provider, the routing agent, API reference, and configuration.

---

## Architecture Overview

Integrations work in **two directions**:

```
┌─────────────────────────────────────────────────────────────┐
│                        INBOUND                              │
│                                                             │
│  GitHub ──webhook──▶  POST /v1/webhooks/github              │
│  GitLab ──webhook──▶  POST /v1/webhooks/gitlab   ──▶  AI   │
│  Jira   ──webhook──▶  POST /v1/webhooks/jira      Pipeline  │
│                                                      │      │
└──────────────────────────────────────────────────────┼──────┘
                                                       │
                                              FeedbackItem
                                              saved to DB
                                                       │
┌──────────────────────────────────────────────────────┼──────┐
│                  7-STAGE AI PIPELINE (Celery)         │      │
│                                                       ▼      │
│  1. Normalize ──▶ 2. Embed ──▶ 3. Dedup ──▶ 4. Classify    │
│       ──▶ 5. Sentiment ──▶ 6. Priority ──▶ 7. Route         │
└─────────────────────────────────────────────────────────────┘
                                                       │
┌──────────────────────────────────────────────────────┼──────┐
│                       OUTBOUND                        │      │
│                                                       ▼      │
│  Slack  ◀── chat.postMessage / incoming webhook             │
│  GitHub ◀── POST /repos/{repo}/issues                       │
└─────────────────────────────────────────────────────────────┘
```

Each tenant's integrations are stored in the `integrations` table. The `credentials` column holds OAuth tokens (sensitive). The `config` column holds non-sensitive settings (selected repos, channel IDs, team IDs) set during the Connect wizard.

---

## The 7-Stage AI Pipeline

Every inbound feedback item — regardless of source — passes through this Celery chain before any outbound routing fires.

### Stage 1 — Normalize (`ingestion.py`)
Strips HTML tags and collapses whitespace from the raw body. Writes an audit log entry.

### Stage 2 — Embed (`ai_processing.py`)
Generates a 384-dimensional vector using `sentence-transformers/all-MiniLM-L6-v2` via the HuggingFace inference API. Stored in `FeedbackItem.embedding` (pgvector). Non-fatal: pipeline continues if embedding fails.

### Stage 3 — Dedup (`deduplication.py`)
Uses cosine similarity on the embedding to detect near-duplicate feedback already in the tenant's dataset. Duplicates are marked `status="duplicate"` and excluded from analytics and digest generation.

### Stage 4 — Classify (`ai_processing.py`)
Categorises the feedback into one of: `bug`, `feature`, `docs`, `performance`, `security`.
- **Primary**: NVIDIA NIM — Llama 3.1 8B (free tier, low latency)
- **Fallback**: Gemini 2.5 Flash

Result stored in `FeedbackItem.category`. A `FeedbackTag` row is written with `confidence_score` and `model_version`.

### Stage 5 — Sentiment (`ai_processing.py`)
VADER sentiment analysis on the normalized body. Returns a compound score from **−1.0** (most negative) to **+1.0** (most positive).

| Band | Range | UI Label |
|---|---|---|
| Frustrated | `< −0.1` | Frustrated |
| Neutral | `−0.1 to 0.1` | Neutral |
| Positive | `>= 0.1` | Positive |

Scores below `−0.6` are logged as critical. Stored in `FeedbackItem.sentiment_score`.

### Stage 6 — Priority (`prioritization.py`)
Asks the LLM to score urgency/impact from 0–100, given title, body, sentiment, and category.
- **Primary**: NVIDIA NIM — Llama 3.1 8B
- **Fallback**: Gemini 2.5 Flash
- **Default on failure**: 50 (medium)

| Score | Level |
|---|---|
| > 80 | Critical |
| > 60 | High |
| 40–60 | Medium |
| < 40 | Low |

Stored in `FeedbackItem.priority_score`.

### Stage 7 — Route (`routing.py`)
Dispatches to all active integrations on the tenant. See the [Routing Agent](#routing-agent) section for full logic.

---

## Providers

### GitHub

**What it does**

| Direction | Trigger | Action |
|---|---|---|
| Inbound | Issue `opened` or `created` webhook event | Creates a `FeedbackItem` and runs the AI pipeline |
| Outbound | `priority_score > 60` AND `category in (bug, feature)` | Creates a GitHub issue with `[BUG]`/`[FEATURE]` prefix and `dfp-automated` label |

**Setup — inbound (webhooks)**

1. Connect via `POST /v1/integrations/github/callback` (OAuth flow saves the token).
2. Select repos with `POST /v1/integrations/github/configure`.
3. In each GitHub repo → Settings → Webhooks:
   - Payload URL: `https://your-domain/v1/webhooks/github`
   - Content type: `application/json`
   - Secret: the value stored in `Integration.webhook_secret` (set this via `PATCH /v1/integrations/github` or directly in the DB)
   - Events: **Issues**, optionally **Issue comments**
4. The `X-Tenant-ID` header must be included — set this in the webhook's custom headers if GitHub supports it, or use a per-tenant webhook URL via a reverse proxy path.

**Credentials stored**
```json
{
  "access_token": "gho_...",
  "token_type": "bearer"
}
```

**Config stored (after /configure)**
```json
{
  "repo_names": ["owner/repo-a", "owner/repo-b"],
  "active_repos": 2
}
```

**Outbound routing — repo selection**

The routing agent posts to every repo in `config.repo_names`. Legacy integrations that stored a single `credentials.repo` string are still supported as a fallback.

---

### Linear

**What it does**

| Direction | Trigger | Action |
|---|---|---|
| Inbound | *(not yet implemented — no webhook handler)* | — |
| Outbound | *(not yet implemented)* | — |

Currently Linear is connect-only: the OAuth flow saves the token and teams are listed for the wizard. Full two-way sync (create issues from high-priority feedback, sync status back) is the next integration milestone.

**Setup**

1. Create a Linear OAuth app at `https://linear.app/settings/api/applications`.
2. Set `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET`, `LINEAR_REDIRECT_URI` in `.env`.
3. Connect via the wizard: `GET /v1/integrations/linear/oauth-url` → authorize → `POST /v1/integrations/linear/callback`.
4. Select teams: `GET /v1/integrations/linear/teams` → `POST /v1/integrations/linear/configure`.

**Credentials stored**
```json
{ "access_token": "lin_api_..." }
```

**Config stored (after /configure)**
```json
{
  "team_ids": ["TEAM-ID-1", "TEAM-ID-2"],
  "mapped_teams": 2
}
```

---

### Slack

**What it does**

| Direction | Trigger | Action |
|---|---|---|
| Inbound | *(not yet implemented — no event handler)* | — |
| Outbound | Every feedback item after pipeline completes | Sends a formatted message to every configured channel |

The outbound message format:
```
🚨 *New Feedback:* Auth token expiry causes silent logout
*Category:* bug
*Priority:* 92.3/100
*Sentiment:* -0.81
*Content:* Users report being silently logged out when...
```
`🚨` is used when `priority_score > 80`, `💬` otherwise.

**Two connection modes**

| Mode | When used | How it works |
|---|---|---|
| Incoming webhook URL | Stored as `credentials.webhook_url` (manual setup) | POSTs JSON directly to the Slack-provided URL |
| Bot token (OAuth) | Stored as `credentials.bot_token` after OAuth flow | Calls `chat.postMessage` for each channel in `config.channel_ids` |

**Setup — bot token (recommended)**

1. Create a Slack app at `https://api.slack.com/apps`.
   - Add OAuth scopes: `channels:read`, `chat:write`, `incoming-webhook`.
2. Set `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_REDIRECT_URI` in `.env`.
3. Connect via wizard: `GET /v1/integrations/slack/oauth-url` → authorize → `POST /v1/integrations/slack/callback`.
4. Select channels: `GET /v1/integrations/slack/channels` → `POST /v1/integrations/slack/configure`.

**Setup — incoming webhook (simpler)**

1. Create an incoming webhook in your Slack app.
2. Call `POST /v1/integrations` with `{ "provider": "slack", "credentials": { "webhook_url": "https://hooks.slack.com/..." } }`.
3. No channel selection needed — the webhook URL is channel-scoped.

**Credentials stored (OAuth)**
```json
{
  "access_token": "xoxb-...",
  "bot_token": "xoxb-...",
  "team_id": "T01234"
}
```

**Config stored (after /configure)**
```json
{
  "channel_ids": ["C01234", "C05678"],
  "active_channels": 2
}
```

---

### GitLab

**What it does**

| Direction | Trigger | Action |
|---|---|---|
| Inbound | Issue `open`/`create` event | Creates a `FeedbackItem` and runs the AI pipeline |
| Outbound | *(not yet implemented)* | — |

**Setup — inbound**

1. Create an integration record with a `webhook_secret`:
   ```
   POST /v1/integrations
   { "provider": "gitlab", "credentials": {} }
   ```
   Then set `webhook_secret` on the row directly (no OAuth UI yet).
2. In GitLab → Settings → Webhooks:
   - URL: `https://your-domain/v1/webhooks/gitlab`
   - Secret Token: value of `Integration.webhook_secret`
   - Triggers: **Issues events**
3. Add `X-Tenant-ID` header matching your tenant.

**Signature verification**: GitLab sends the secret as the `X-Gitlab-Token` header (compared with `hmac.compare_digest`, constant-time).

---

### Jira

**What it does**

| Direction | Trigger | Action |
|---|---|---|
| Inbound | Issue event webhook | Creates a `FeedbackItem` and runs the AI pipeline |
| Outbound | *(coming soon — marked in UI)* | — |

**Setup — inbound**

1. Create an integration record:
   ```
   POST /v1/integrations
   { "provider": "jira", "credentials": {} }
   ```
   Set `webhook_secret` on the row.
2. In Jira → System → Webhooks:
   - URL: `https://your-domain/v1/webhooks/jira`
   - Events: **Issue created**, **Issue updated**
3. Jira does not natively support custom request headers — route through a proxy that injects `X-Tenant-ID` and `X-Jira-Token`.

**Signature verification**: `X-Jira-Token` header compared with `Integration.webhook_secret` (constant-time).

---

### Discord

Available in the UI as "Not Connected". OAuth and webhook handler are not yet implemented. Planned: capture bug reports from Discord server channels via the Discord bot API.

---

## Routing Agent

The routing agent (`agents/routing.py`) runs as Stage 7 of the Celery pipeline. It loads every `active` integration for the tenant and dispatches based on provider-specific rules.

### Slack routing rules

| Condition | Behaviour |
|---|---|
| `credentials.webhook_url` set | POST directly to the incoming webhook URL |
| `credentials.bot_token` or `credentials.access_token` set | Call `chat.postMessage` for each `config.channel_ids` |
| Neither set | Skip silently |
| `priority_score > 80` | Prefix emoji `🚨` |
| Otherwise | Prefix emoji `💬` |

### GitHub routing rules

| Condition | Behaviour |
|---|---|
| `priority_score < 60` | Skip — not urgent enough to create a tracked issue |
| `category not in (bug, feature)` | Skip — docs/performance/security don't map to issues |
| `config.repo_names` empty AND `credentials.repo` empty | Skip — no repo configured |
| Otherwise | Create a GitHub issue in every repo in `config.repo_names` |

---

## Connect Wizard — API Flow

```
Step 1 — Authorize
  GET  /v1/integrations/providers              → provider catalog
  GET  /v1/integrations/{provider}/oauth-url   → { url, state }
  [browser redirects to provider OAuth page]
  POST /v1/integrations/{provider}/callback?code=&state=
       → exchanges code, saves credentials, returns IntegrationResponse

Step 2 — Select resources
  GET  /v1/integrations/github/repos           → list of repos
  GET  /v1/integrations/linear/teams           → list of teams
  GET  /v1/integrations/slack/channels         → list of channels

Step 3 — Rules
  GET  /v1/integrations/routing-rules          → existing rules
  POST /v1/integrations/routing-rules          → create a rule

Step 4 — Finish
  POST /v1/integrations/{provider}/configure
  Body: { "selections": { "repos": [...] }, "routing_rules": [...] }
       → saves config, updates meta stats (active_repos / mapped_teams / active_channels)
```

OAuth state is a `secrets.token_urlsafe(32)` token stored in Redis for 10 minutes and deleted after one use. If Redis is unavailable the check is skipped (fail-open) so the flow still completes.

---

## Integrations List Page — API Flow

```
GET /v1/integrations
```

Returns the full provider catalog (GitHub, Linear, Slack, Discord, Jira) merged with the tenant's connected rows:

```json
[
  {
    "provider": "github",
    "name": "GitHub",
    "status": "connected",
    "last_sync_at": "2024-06-07T10:00:00Z",
    "meta": { "active_repos": 12, "repo_names": ["owner/repo"] },
    "coming_soon": false
  },
  {
    "provider": "discord",
    "name": "Discord",
    "status": "not_connected",
    "last_sync_at": null,
    "meta": null,
    "coming_soon": false
  }
]
```

`meta` is populated from `integration.config` which is updated every time `/configure` is called.

To disconnect: `DELETE /v1/integrations/{provider}` — removes the row and all credentials.

---

## Webhook Security

All inbound webhook endpoints verify that the request came from the expected provider before touching the database.

| Provider | Method | Header |
|---|---|---|
| GitHub | HMAC-SHA256 | `X-Hub-Signature-256: sha256=<hex>` |
| GitLab | Constant-time string compare | `X-Gitlab-Token: <secret>` |
| Jira | Constant-time string compare | `X-Jira-Token: <secret>` |

All use `hmac.compare_digest` to prevent timing attacks. A webhook with a missing or non-configured `webhook_secret` returns **500** immediately — it does not fall through to unauthenticated processing.

Payload size is capped at **64 KB**. Requests exceeding this return **413**.

---

## Configuration Reference

Add to `.env` (see `.env.example` for the full template):

```env
# GitHub (user auth + integration share the same app)
github_client_id=
github_client_secret=
github_redirect_uri=                    # user OAuth callback
github_integration_redirect_uri=       # integration wizard callback

# Linear
linear_client_id=
linear_client_secret=
linear_redirect_uri=

# Slack
slack_client_id=
slack_client_secret=
slack_redirect_uri=
```

---

## Data Model

```
integrations
  id               UUID PK
  tenant_id        FK → tenants.id
  provider         github | linear | slack | discord | gitlab | jira
  credentials      JSON  — OAuth tokens (encrypt in production, see Issue #13)
  config           JSON  — non-sensitive: repo_names, channel_ids, team_ids, display stats
  webhook_secret   TEXT  — HMAC secret for inbound webhook verification
  status           active | inactive | error
  last_sync_at     TIMESTAMPTZ
  created_at       TIMESTAMPTZ
  UNIQUE (tenant_id, provider)
```

---

## Bugs Fixed

| # | Location | Bug | Fix |
|---|---|---|---|
| 1 | `agents/routing.py` `_route_to_slack` | Read `credentials["webhook_url"]` only — OAuth flow stores `bot_token`, so Slack routing always silently skipped | Now supports both modes: incoming webhook URL and bot token via `chat.postMessage` to each `config.channel_ids` |
| 2 | `agents/routing.py` `_route_to_github` | Read `credentials["repo"]` (single string) — configure step stores `config["repo_names"]` (list), so GitHub routing always silently skipped | Now reads `config.repo_names`, falls back to `credentials.repo`, posts to every repo in the list |
| 3 | `api/v1/analytics.py` | Sentiment thresholds `0.3` / `0.6` treated VADER scores as 0–1 scale | VADER produces −1.0 to +1.0 — thresholds corrected to `−0.1` (frustrated) / `0.1` (positive) |
