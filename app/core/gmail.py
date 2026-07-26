import base64
import time

import httpx

from app.config import settings

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"


async def refresh_access_token(refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token. Raises ValueError on failure."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    data = resp.json() if resp.status_code == 200 else {}
    if "access_token" not in data:
        raise ValueError(f"Gmail token refresh failed: {data}")
    return data


async def get_valid_access_token(credentials: dict) -> tuple[str, dict | None]:
    """
    Return a valid access token, refreshing it first if it's expired (or about to be).
    The second return value is the updated credentials dict when a refresh happened,
    or None when the stored token was still valid — callers use this to decide
    whether the Integration row needs to be persisted.
    """
    if time.time() < credentials.get("expires_at", 0) - 60:
        return credentials["access_token"], None

    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        raise ValueError("Gmail credentials missing refresh_token — reconnect the integration")

    data = await refresh_access_token(refresh_token)
    updated = dict(credentials)
    updated["access_token"] = data["access_token"]
    updated["expires_at"] = time.time() + data.get("expires_in", 3600)
    return updated["access_token"], updated


async def list_labels(access_token: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/labels",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise ValueError("Failed to fetch Gmail labels")
    return resp.json().get("labels", [])


async def list_message_ids(access_token: str, query: str, max_results: int = 50) -> list[str]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/messages",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"q": query, "maxResults": max_results},
        )
    if resp.status_code != 200:
        raise ValueError(f"Failed to list Gmail messages: {resp.text}")
    return [m["id"] for m in resp.json().get("messages", [])]


def _decode_part(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Walk the MIME tree and return the first text/plain part (falls back to text/html)."""
    if payload.get("mimeType", "").startswith("text/") and payload.get("body", {}).get("data"):
        return _decode_part(payload["body"]["data"])

    parts = payload.get("parts") or []
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode_part(part["body"]["data"])
    for part in parts:
        body = _extract_body(part)
        if body:
            return body
    return ""


async def get_message(access_token: str, message_id: str) -> dict:
    """Fetch a single Gmail message and parse it into title/body/sender fields."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"format": "full"},
        )
    if resp.status_code != 200:
        raise ValueError(f"Failed to fetch Gmail message {message_id}")

    data = resp.json()
    payload = data.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    return {
        "id": data["id"],
        "thread_id": data.get("threadId", ""),
        "subject": headers.get("subject", "(no subject)"),
        "from": headers.get("from", ""),
        "body": _extract_body(payload) or data.get("snippet", ""),
        "label_ids": data.get("labelIds", []),
    }
