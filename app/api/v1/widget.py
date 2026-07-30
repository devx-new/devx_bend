"""
Widget integration API.

Two route groups:
  - /v1/widget/keys  (auth-protected) — create, list, revoke API keys for the JS widget
  - /v1/widget/ingest (public)        — accept feedback posted by the embedded widget
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.relevance import is_noise_by_rules
from app.api.deps import get_current_user
from app.core.rate_limit import check_rate_limit
from app.database import get_db
from app.models.audit import AuditLog
from app.models.feedback import FeedbackItem
from app.models.user import User
from app.models.widget_key import WidgetKey
from app.worker.orchestrator import start_feedback_pipeline

router = APIRouter(prefix="/widget", tags=["widget"])
logger = logging.getLogger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────────────

class WidgetKeyCreate(BaseModel):
    label: str
    allowed_origins: list[str] | None = None


class WidgetKeyResponse(BaseModel):
    id: str
    label: str
    key_prefix: str
    allowed_origins: list[str] | None
    status: str
    created_at: datetime
    last_used_at: datetime | None

    model_config = {"from_attributes": True}


class WidgetKeyCreated(WidgetKeyResponse):
    key: str  # full key — returned only on creation


class IngestPayload(BaseModel):
    title: str
    body: str | None = None
    user_handle: str | None = None


# ── Audit helper ──────────────────────────────────────────────────────────────

async def _audit(
    db: AsyncSession,
    tenant_id: str,
    actor_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    diff: dict | None = None,
    ip: str | None = None,
) -> None:
    db.add(AuditLog(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        diff=diff,
        ip=ip,
    ))
    await db.flush()


# ── Key management (requires user session) ───────────────────────────────────

@router.post("/keys", response_model=WidgetKeyCreated, status_code=201)
async def create_widget_key(
    body: WidgetKeyCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a new widget API key. The full key is returned once — store it securely."""
    full_key, prefix, key_hash = WidgetKey.generate()
    wk = WidgetKey(
        tenant_id=current_user.tenant_id,
        label=body.label,
        key_prefix=prefix,
        key_hash=key_hash,
        allowed_origins=body.allowed_origins or [],
    )
    db.add(wk)
    await db.flush()

    ip = request.client.host if request.client else None
    await _audit(
        db,
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="CREATE",
        resource_type="widget_key",
        resource_id=wk.id,
        diff={"label": body.label, "allowed_origins": body.allowed_origins or [], "key_prefix": prefix},
        ip=ip,
    )

    await db.commit()
    await db.refresh(wk)

    logger.info(
        "Widget key created",
        extra={"tenant_id": current_user.tenant_id, "actor_id": current_user.id,
               "key_id": wk.id, "label": body.label, "ip": ip},
    )

    return WidgetKeyCreated(
        id=wk.id,
        label=wk.label,
        key_prefix=wk.key_prefix,
        allowed_origins=wk.allowed_origins,
        status=wk.status,
        created_at=wk.created_at,
        last_used_at=wk.last_used_at,
        key=full_key,
    )


@router.get("/keys")
async def list_widget_keys(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(WidgetKey)
        .where(
            WidgetKey.tenant_id == current_user.tenant_id,
            WidgetKey.status == "active",
        )
        .order_by(WidgetKey.created_at)
    )
    keys = result.scalars().all()
    return {
        "success": True,
        "data": [WidgetKeyResponse.model_validate(k).model_dump() for k in keys],
    }


@router.delete("/keys/{key_id}", status_code=200)
async def revoke_widget_key(
    key_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(WidgetKey).where(
            WidgetKey.id == key_id,
            WidgetKey.tenant_id == current_user.tenant_id,
        )
    )
    wk = result.scalar_one_or_none()
    if not wk:
        raise HTTPException(status_code=404, detail="Widget key not found")

    wk.status = "revoked"

    ip = request.client.host if request.client else None
    await _audit(
        db,
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="REVOKE",
        resource_type="widget_key",
        resource_id=wk.id,
        diff={"label": wk.label, "key_prefix": wk.key_prefix},
        ip=ip,
    )

    await db.commit()

    logger.info(
        "Widget key revoked",
        extra={"tenant_id": current_user.tenant_id, "actor_id": current_user.id,
               "key_id": wk.id, "label": wk.label, "ip": ip},
    )

    return {"success": True, "message": "Key revoked"}


# ── Public ingest endpoint ────────────────────────────────────────────────────

async def _resolve_widget_key(request: Request, db: AsyncSession) -> WidgetKey:
    """Validate the Bearer token and return the matching WidgetKey. Raises 401 on failure."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Widget ingest rejected — missing or malformed Authorization header",
            extra={"ip": ip, "origin": request.headers.get("origin", "")},
        )
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    full_key = auth.removeprefix("Bearer ").strip()
    key_hash = WidgetKey.hash_key(full_key)

    result = await db.execute(
        select(WidgetKey).where(
            WidgetKey.key_hash == key_hash,
            WidgetKey.status == "active",
        )
    )
    wk = result.scalar_one_or_none()
    if not wk:
        ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Widget ingest rejected — invalid or revoked key",
            extra={"ip": ip, "origin": request.headers.get("origin", ""),
                   "key_prefix": full_key[:16] if len(full_key) >= 16 else "short"},
        )
        raise HTTPException(status_code=401, detail="Invalid or revoked widget key")

    return wk


def _check_origin(request: Request, wk: WidgetKey) -> None:
    """
    Enforce origin policy on browser-initiated requests.

    - No Origin header (server-to-server, curl, mobile): always allowed.
    - Origin header present + allowed_origins configured: must be in the list.
    - Origin header present + allowed_origins empty/unset: rejected.
      An empty list means the key was not configured for browser use.
      Set allowed_origins explicitly to permit browser embeds.
    """
    origin = request.headers.get("origin", "")
    if not origin:
        return  # non-browser caller — no restriction applies

    if not wk.allowed_origins:
        ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Widget ingest rejected — key has no allowed_origins (browser use not configured)",
            extra={"ip": ip, "origin": origin, "key_id": wk.id, "tenant_id": wk.tenant_id},
        )
        raise HTTPException(
            status_code=403,
            detail="This key is not configured for browser use. Add allowed_origins to permit embed requests.",
        )

    if origin.rstrip("/") not in [o.rstrip("/") for o in wk.allowed_origins]:
        ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Widget ingest rejected — origin not in allowlist",
            extra={"ip": ip, "origin": origin, "key_id": wk.id, "tenant_id": wk.tenant_id,
                   "allowed_origins": wk.allowed_origins},
        )
        raise HTTPException(status_code=403, detail="Origin not allowed for this widget key")


@router.post("/ingest", status_code=202)
async def widget_ingest(
    payload: IngestPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint for the embeddable widget.
    Authenticate with:  Authorization: Bearer pk_live_<key>
    """
    wk = await _resolve_widget_key(request, db)
    _check_origin(request, wk)

    # Per-key rate limit: 60 submissions / minute
    allowed = await check_rate_limit(wk.id, "widget.ingest", max_requests=60, window_seconds=60)
    if not allowed:
        ip = request.client.host if request.client else "unknown"
        logger.warning(
            "Widget ingest rate limited",
            extra={"ip": ip, "key_id": wk.id, "tenant_id": wk.tenant_id,
                   "origin": request.headers.get("origin", "")},
        )
        raise HTTPException(status_code=429, detail="Rate limit exceeded for this widget key")

    title = payload.title.strip()[:500]
    if not title:
        raise HTTPException(status_code=422, detail="title is required")
    if len(title) < 10:
        raise HTTPException(status_code=422, detail="title must be at least 10 characters")

    body_text = (payload.body or "").strip()
    if body_text and len(body_text) < 10:
        raise HTTPException(status_code=422, detail="description must be at least 10 characters if provided")

    # The widget is a purpose-built feedback form — unlike an inbox/channel, nearly
    # everything submitted through it is intentional feedback, so we only run the
    # cheap deterministic noise guard here, not the LLM relevance check (which is
    # tuned for high-noise ambient sources like Gmail/Slack and produces false
    # negatives on short, legitimate praise/complaints such as "Nice UI look").
    noise_check = {"from": payload.user_handle or "", "label_ids": [], "body": body_text or title}
    if is_noise_by_rules(noise_check):
        logger.info(
            "Widget submission classified as noise — accepted but not queued",
            extra={"tenant_id": wk.tenant_id, "key_id": wk.id},
        )
        return {"success": True, "data": {"id": None}}

    ip = request.client.host if request.client else None
    external_id = f"widget-{wk.id}-{uuid.uuid4()}"
    item = FeedbackItem(
        tenant_id=wk.tenant_id,
        source="widget",
        external_id=external_id,
        title=title,
        body=body_text or title,
        author_handle=payload.user_handle,
    )
    db.add(item)
    await db.flush()

    await _audit(
        db,
        tenant_id=wk.tenant_id,
        actor_id=None,  # public endpoint — no authenticated user
        action="CREATE",
        resource_type="feedback_item",
        resource_id=item.id,
        diff={"source": "widget", "key_id": wk.id, "key_prefix": wk.key_prefix,
              "title": title, "author_handle": payload.user_handle,
              "origin": request.headers.get("origin", "")},
        ip=ip,
    )

    wk.last_used_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(item)

    logger.info(
        "Widget feedback ingested",
        extra={"tenant_id": wk.tenant_id, "key_id": wk.id, "feedback_id": item.id,
               "origin": request.headers.get("origin", ""), "ip": ip},
    )

    start_feedback_pipeline.delay(item.id, wk.tenant_id)

    return {"success": True, "data": {"id": item.id}}
