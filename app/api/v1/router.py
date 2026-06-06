from fastapi import APIRouter

from app.api.v1 import auth, feedback, analytics, integrations, webhooks

router = APIRouter(prefix="/v1")
router.include_router(auth.router)
router.include_router(feedback.router)
router.include_router(analytics.router)
router.include_router(integrations.router)
router.include_router(webhooks.router)
