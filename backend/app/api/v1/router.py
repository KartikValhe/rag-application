"""app/api/v1/router.py — Aggregate all v1 route modules."""

from fastapi import APIRouter

from app.api.v1 import admin, auth, platform

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(platform.router)
router.include_router(admin.router)

