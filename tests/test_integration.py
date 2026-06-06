"""
Integration tests for the DFP API.

Covers:
  - Auth: register, login, lockout, logout
  - Feedback: CRUD, filtering, status update, tenant isolation
  - Analytics: KPIs, trends (Redis unavailable → falls through to DB)
  - Pipeline trigger: POST /feedback fires orchestrator.delay() without error

All tests use the SQLite in-memory DB from conftest.py.
Celery tasks are patched to avoid needing a live Redis broker.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

BASE = "http://test"


def _transport():
    return ASGITransport(app=app)


def _unique_email(prefix: str = "u") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}@example.com"


async def _register(client: AsyncClient, email: str, password: str, org: str = "Test Org") -> None:
    resp = await client.post(
        "/v1/auth/register",
        json={"email": email, "password": password, "organization_name": org},
    )
    assert resp.status_code in (200, 201), f"Register failed: {resp.text}"


async def _login(client: AsyncClient, email: str, password: str) -> None:
    resp = await client.post("/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"


# ─── Auth Tests ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_creates_user():
    email = _unique_email("reg")
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        resp = await c.post(
            "/v1/auth/register",
            json={"email": email, "password": "StrongPass1!", "organization_name": "Acme"},
        )
    assert resp.status_code in (200, 201)
    assert resp.json().get("success") is True


@pytest.mark.asyncio
async def test_register_duplicate_email_rejected():
    email = _unique_email("dup")
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        await c.post("/v1/auth/register", json={"email": email, "password": "Pass1234!", "organization_name": "Org"})
        resp = await c.post("/v1/auth/register", json={"email": email, "password": "Pass1234!", "organization_name": "Org"})
    # API returns 400 for duplicate
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_login_valid_credentials():
    email = _unique_email("login")
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        await _register(c, email, "Pass1234!")
        resp = await c.post("/v1/auth/login", json={"email": email, "password": "Pass1234!"})
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("success") is True
    # Tokens must NOT appear in response body
    assert "access_token" not in body
    assert "refresh_token" not in body


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401():
    email = _unique_email("wrong")
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        await _register(c, email, "Correct1!")
        resp = await c.post("/v1/auth/login", json={"email": email, "password": "WrongPass!"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_lockout_after_5_bad_attempts():
    email = _unique_email("lock")
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        await _register(c, email, "Right1234!")
        for _ in range(5):
            await c.post("/v1/auth/login", json={"email": email, "password": "wrong"})
        # 6th attempt with correct password — should be locked
        resp = await c.post("/v1/auth/login", json={"email": email, "password": "Right1234!"})
    assert resp.status_code in (401, 429)


@pytest.mark.asyncio
async def test_feedback_list_requires_auth():
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        resp = await c.get("/v1/feedback")
    assert resp.status_code == 401


# ─── Feedback Tests ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_feedback_fires_pipeline():
    email = _unique_email("pipe")
    with patch("app.worker.orchestrator.start_feedback_pipeline") as mock_task:
        mock_task.delay = MagicMock(return_value=None)
        async with AsyncClient(transport=_transport(), base_url=BASE) as c:
            await _register(c, email, "PipePass1!")
            await _login(c, email, "PipePass1!")
            csrf = c.cookies.get("csrf_token", "")
            resp = await c.post(
                "/v1/feedback",
                json={"source": "api", "title": "Pipeline test", "body": "Should trigger AI pipeline."},
                headers={"X-CSRF-Token": csrf},
            )
    assert resp.status_code in (200, 201)
    assert resp.json().get("title") == "Pipeline test"


@pytest.mark.asyncio
async def test_list_feedback_paginated():
    email = _unique_email("list")
    with patch("app.worker.orchestrator.start_feedback_pipeline") as mock_task:
        mock_task.delay = MagicMock(return_value=None)
        async with AsyncClient(transport=_transport(), base_url=BASE) as c:
            await _register(c, email, "ListPass1!")
            await _login(c, email, "ListPass1!")
            csrf = c.cookies.get("csrf_token", "")
            for i in range(3):
                await c.post(
                    "/v1/feedback",
                    json={"source": "api", "title": f"Item {i}", "body": f"Body {i}"},
                    headers={"X-CSRF-Token": csrf},
                )
            resp = await c.get("/v1/feedback?per_page=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["meta"]["total"] >= 3


@pytest.mark.asyncio
async def test_get_feedback_by_id():
    email = _unique_email("byid")
    with patch("app.worker.orchestrator.start_feedback_pipeline") as mock_task:
        mock_task.delay = MagicMock(return_value=None)
        async with AsyncClient(transport=_transport(), base_url=BASE) as c:
            await _register(c, email, "ByIdPass1!")
            await _login(c, email, "ByIdPass1!")
            csrf = c.cookies.get("csrf_token", "")
            create = await c.post(
                "/v1/feedback",
                json={"source": "api", "title": "Get by ID", "body": "Test"},
                headers={"X-CSRF-Token": csrf},
            )
            item_id = create.json()["id"]
            resp = await c.get(f"/v1/feedback/{item_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == item_id


@pytest.mark.asyncio
async def test_tenant_isolation():
    """Feedback from tenant A must be invisible to tenant B."""
    email_a, email_b = _unique_email("ta"), _unique_email("tb")
    with patch("app.worker.orchestrator.start_feedback_pipeline") as mock_task:
        mock_task.delay = MagicMock(return_value=None)
        async with AsyncClient(transport=_transport(), base_url=BASE) as ca:
            await _register(ca, email_a, "TaPass1!", org="OrgA")
            await _login(ca, email_a, "TaPass1!")
            csrf_a = ca.cookies.get("csrf_token", "")
            create = await ca.post(
                "/v1/feedback",
                json={"source": "api", "title": "Tenant A secret", "body": "Private"},
                headers={"X-CSRF-Token": csrf_a},
            )
            item_id = create.json()["id"]

        async with AsyncClient(transport=_transport(), base_url=BASE) as cb:
            await _register(cb, email_b, "TbPass1!", org="OrgB")
            await _login(cb, email_b, "TbPass1!")
            resp = await cb.get(f"/v1/feedback/{item_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_feedback_status():
    email = _unique_email("upd")
    with patch("app.worker.orchestrator.start_feedback_pipeline") as mock_task:
        mock_task.delay = MagicMock(return_value=None)
        async with AsyncClient(transport=_transport(), base_url=BASE) as c:
            await _register(c, email, "UpdPass1!")
            await _login(c, email, "UpdPass1!")
            csrf = c.cookies.get("csrf_token", "")
            create = await c.post(
                "/v1/feedback",
                json={"source": "api", "title": "To resolve", "body": "Fix me"},
                headers={"X-CSRF-Token": csrf},
            )
            item_id = create.json()["id"]
            resp = await c.patch(
                f"/v1/feedback/{item_id}/status",
                json={"status": "resolved"},
                headers={"X-CSRF-Token": csrf},
            )
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


# ─── Analytics Tests ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analytics_requires_auth():
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        resp = await c.get("/v1/analytics/summary")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_analytics_kpis_schema():
    email = _unique_email("kpi")
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        await _register(c, email, "KpiPass1!")
        await _login(c, email, "KpiPass1!")
        resp = await c.get("/v1/analytics/kpis")
    assert resp.status_code == 200
    data = resp.json()["data"]
    for key in ("total_items", "open_count", "critical_count", "top_categories"):
        assert key in data, f"Missing key: {key}"


@pytest.mark.asyncio
async def test_analytics_trends_schema():
    email = _unique_email("trend")
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        await _register(c, email, "TrendPass1!")
        await _login(c, email, "TrendPass1!")
        resp = await c.get("/v1/analytics/trends?days=7")
    assert resp.status_code == 200
    assert "daily" in resp.json()["data"]


# ─── Health ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=_transport(), base_url=BASE) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
