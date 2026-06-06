"""
Seed script — populates the database with a test tenant, admin user,
mock integrations, and 50 historical feedback items.

Usage:
    PYTHONPATH=. python scripts/seed.py
    # or via Makefile:
    make seed
"""

import asyncio
import random
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 — ensures all models registered with Base
from app.database import async_session_factory, engine
from app.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.integration import Integration
from app.models.feedback import FeedbackItem
from app.security import hash_password

CATEGORIES = ["bug", "feature", "docs", "performance", "security", "uncategorized"]
SOURCES = ["github", "slack", "portal", "jira"]
SAMPLE_FEEDBACK = [
    ("Login page crashes on Safari", "When I click Sign In on Safari 16, the page reloads without logging in."),
    ("Add dark mode support", "Would love a dark mode toggle in the settings panel."),
    ("API response time is very slow", "The /feedback endpoint takes >5s on large result sets."),
    ("Docs missing OAuth2 example", "The API docs don't show how to authenticate with OAuth2 bearer tokens."),
    ("SQL injection possible in search", "The search query parameter is not properly sanitized."),
    ("Export to CSV feature request", "It would be useful to export the feedback table to CSV."),
    ("Pagination is broken on mobile", "On mobile the pagination buttons overlap with footer content."),
    ("Rate limiter returns 500 instead of 429", "When rate limited, the server throws a 500 error."),
    ("Add webhook retry mechanism", "Failed webhooks should be retried with exponential backoff."),
    ("Sorting by priority doesn't persist", "After refreshing the page, the sort order resets to default."),
    ("Email notifications are delayed by hours", "I'm getting email alerts several hours after the event."),
    ("No error message when upload fails", "File upload silently fails with no feedback to the user."),
    ("Allow bulk status update", "Need a way to mark multiple feedback items as resolved at once."),
    ("Memory leak in Celery worker", "The Celery worker consumes more memory over time and needs to be restarted."),
    ("CORS headers missing on error responses", "404 and 500 responses don't include CORS headers, breaking the frontend."),
]


async def seed():
    async with engine.begin() as conn:
        print("Creating tables if they don't exist...")
        await conn.run_sync(Base.metadata.create_all)

    async with async_session_factory() as session:
        # ── Tenant ────────────────────────────────────────────────────────────
        tenant = Tenant(
            id=str(uuid.uuid4()),
            name="Acme Corp",
            slug="acme-corp",
            config={},
            plan_tier="pro",
        )
        session.add(tenant)
        await session.flush()
        print(f"✓ Tenant created: {tenant.name} ({tenant.id})")

        # ── Admin User ────────────────────────────────────────────────────────
        user = User(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            email="admin@acme.com",
            password_hash=hash_password("Admin1234!"),
            role="admin",
        )
        session.add(user)
        await session.flush()
        print(f"✓ Admin user created: {user.email} / password: Admin1234!")

        # ── Integrations ──────────────────────────────────────────────────────
        slack_integration = Integration(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            provider="slack",
            credentials={"webhook_url": "https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK"},
            status="active",
        )
        github_integration = Integration(
            id=str(uuid.uuid4()),
            tenant_id=tenant.id,
            provider="github",
            credentials={
                "access_token": "github_pat_YOUR_TOKEN_HERE",
                "repo": "your-org/your-repo",
            },
            webhook_secret="your-github-webhook-secret",
            status="active",
        )
        session.add_all([slack_integration, github_integration])
        print("✓ Integrations created: slack, github")

        # ── Feedback Items ────────────────────────────────────────────────────
        now = datetime.now(timezone.utc)
        for i in range(50):
            title, body = random.choice(SAMPLE_FEEDBACK)
            days_ago = random.randint(0, 30)
            priority = random.uniform(10, 99)
            sentiment = random.uniform(-1.0, 1.0)

            item = FeedbackItem(
                id=str(uuid.uuid4()),
                tenant_id=tenant.id,
                source=random.choice(SOURCES),
                external_id=f"SEED-{i+1:04d}",
                title=f"{title} #{i+1}",
                body=body,
                author_handle=f"dev{random.randint(1, 20)}@acme.com",
                status=random.choice(["open", "open", "open", "resolved"]),
                category=random.choice(CATEGORIES),
                sentiment_score=round(sentiment, 3),
                priority_score=round(priority, 1),
                created_at=now - timedelta(days=days_ago),
            )
            session.add(item)

        await session.commit()
        print("✓ 50 seed feedback items created")

    print("\n🎉 Seed complete!")
    print(f"   Tenant ID : {tenant.id}")
    print(f"   User email: admin@acme.com")
    print(f"   Password  : Admin1234!")
    print("\n   Update the Slack webhook URL and GitHub token in the integrations table.")
    print("   Run: uvicorn app.main:app --reload  then visit http://localhost:8000/docs")


if __name__ == "__main__":
    asyncio.run(seed())
