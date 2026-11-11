"""app/api/v1/router.py — Aggregate all v1 route modules."""

from fastapi import APIRouter

from app.api.v1 import academic, admin, audit, auth, branding, platform, roles, timetable

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(platform.router)
router.include_router(admin.router)
router.include_router(roles.router)
router.include_router(academic.router)
router.include_router(timetable.router)
router.include_router(audit.router)
router.include_router(branding.public_router)
router.include_router(branding.admin_router)

