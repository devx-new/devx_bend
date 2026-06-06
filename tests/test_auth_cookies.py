"""
Cookie-based authentication tests.

Tests for:
  - login body contains no token strings (even on failure)
  - login invalid credentials returns 401 with no cookies
  - CSRF validation: missing header → 403, wrong value → 403, correct → not 403
  - GET requests are CSRF-exempt
  - logout returns success and attempts to clear cookies
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


# ---------------------------------------------------------------------------
# Tests: login response shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_login_body_never_contains_tokens():
    """Login response body must not expose raw JWT strings regardless of outcome."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong"},
        )
    body = resp.json()
    assert "access_token" not in body
    assert "refresh_token" not in body


@pytest.mark.asyncio
async def test_login_invalid_credentials_returns_401():
    """Bad credentials → 401, no auth cookies set on the response."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/v1/auth/login",
            json={"email": "nobody@example.com", "password": "wrong"},
        )
    assert resp.status_code == 401
    # No auth cookies should be set on a failed login
    cookie_names = {c.name for c in resp.cookies.jar}
    assert "access_token" not in cookie_names
    assert "refresh_token" not in cookie_names
    assert "csrf_token" not in cookie_names


# ---------------------------------------------------------------------------
# Tests: CSRF validation on a POST endpoint (no rate limiter)
# We test on PATCH /v1/feedback/{id}/status — requires get_current_user
# but has no RateLimitDep, so Redis is never touched.
# ---------------------------------------------------------------------------

CSRF_ENDPOINT = "/v1/feedback/nonexistent-id/status"
CSRF_BODY = {"status": "resolved"}


@pytest.mark.asyncio
async def test_post_without_csrf_header_returns_403():
    """POST with access_token cookie but no X-CSRF-Token header → 403."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("access_token", "fake.jwt.token")
        client.cookies.set("csrf_token", "expected-csrf-value")
        resp = await client.patch(
            CSRF_ENDPOINT,
            json=CSRF_BODY,
            headers={"X-Tenant-ID": "test-tenant"},
            # deliberately omit X-CSRF-Token
        )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_post_with_wrong_csrf_value_returns_403():
    """X-CSRF-Token header present but mismatched with cookie → 403."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("access_token", "fake.jwt.token")
        client.cookies.set("csrf_token", "correct-csrf-value")
        resp = await client.patch(
            CSRF_ENDPOINT,
            json=CSRF_BODY,
            headers={
                "X-Tenant-ID": "test-tenant",
                "X-CSRF-Token": "WRONG-csrf-value",
            },
        )
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_post_with_correct_csrf_passes_csrf_gate():
    """Matching X-CSRF-Token clears the CSRF gate; subsequent failure is 401 not 403."""
    csrf_value = "a" * 64
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        client.cookies.set("access_token", "fake.jwt.token")
        client.cookies.set("csrf_token", csrf_value)
        resp = await client.patch(
            CSRF_ENDPOINT,
            json=CSRF_BODY,
            headers={
                "X-Tenant-ID": "test-tenant",
                "X-CSRF-Token": csrf_value,  # matches cookie exactly
            },
        )
    # CSRF passes → JWT decode fails on "fake.jwt.token" → 401
    assert resp.status_code == 401
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_get_request_requires_no_csrf():
    """GET /v1/feedback without X-CSRF-Token must fail with 401 (no auth), NOT 403 (CSRF)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/feedback",
            headers={"X-Tenant-ID": "test-tenant"},
        )
    # GET is CSRF-exempt; failure is 401 (no cookie), not 403
    assert resp.status_code == 401
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Tests: logout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logout_returns_success_without_refresh_cookie():
    """Logout must succeed gracefully even when no refresh_token cookie is present."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"success": True, "message": {"logged_out": True}}


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
