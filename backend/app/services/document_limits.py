"""
app/services/document_limits.py
───────────────────────────────
Per-tenant document limits (design A2, decision C6-5).

The effective limits of a tenant are the platform defaults (environment settings) with the tenant's
own overrides from `tenants.doc_limits` on top. Every value must be a positive integer and may never
exceed its hard platform ceiling below; unknown keys are refused. The same validation runs when a
superadmin writes overrides and when the API reads them, so a bad stored value fails closed (the
upload is refused) instead of silently raising a limit.

Only the superadmin endpoint `PATCH /api/v1/platform/tenants/{tenant_id}/limits` writes overrides
(app_platform). The API reads them through the tenant session: app_tenant may read the
`doc_limits` column of its own row only (migration 0011 plus the tenant_self_select policy).
"""

import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.upload_security import DOC_UPLOAD_CEILING_BYTES
from app.db.models.tenant import Tenant

LIMIT_KEYS: tuple[str, ...] = (
    "max_file_bytes",
    "max_pdf_pages",
    "max_pptx_slides",
    "max_documents",
    "max_storage_bytes",
    "max_pages",
    "uploads_per_user_per_hour",
    "uploads_per_tenant_per_hour",
)

# Hard platform ceilings. No default and no tenant override may go above these. The file ceiling is
# the same 50 MB the body-limit middleware and stream_upload_with_limit enforce.
PLATFORM_CEILINGS: dict[str, int] = {
    "max_file_bytes": DOC_UPLOAD_CEILING_BYTES,
    "max_pdf_pages": 2000,
    "max_pptx_slides": 1000,
    "max_documents": 10_000,
    "max_storage_bytes": 20 * 1024 * 1024 * 1024,
    "max_pages": 200_000,
    "uploads_per_user_per_hour": 500,
    "uploads_per_tenant_per_hour": 5000,
}

_SETTING_FOR_KEY: dict[str, str] = {
    "max_file_bytes": "DOC_MAX_FILE_BYTES",
    "max_pdf_pages": "DOC_MAX_PDF_PAGES",
    "max_pptx_slides": "DOC_MAX_PPTX_SLIDES",
    "max_documents": "QUOTA_MAX_DOCUMENTS",
    "max_storage_bytes": "QUOTA_MAX_STORAGE_BYTES",
    "max_pages": "QUOTA_MAX_PAGES",
    "uploads_per_user_per_hour": "DOC_UPLOAD_RATE_USER",
    "uploads_per_tenant_per_hour": "DOC_UPLOAD_RATE_TENANT",
}

_MESSAGES: dict[str, str] = {
    "unknown_limit": "Unknown limit name.",
    "invalid_limit_value": "Limit values must be positive whole numbers.",
    "limit_above_ceiling": "A limit is above the platform maximum.",
}


class DocumentLimitsError(ValueError):
    """A limit name or value is not allowed. `code` is the stable API error code."""

    def __init__(self, code: str, key: str) -> None:
        super().__init__(f"{code}: {key}")
        self.code = code
        self.key = key
        self.message = _MESSAGES[code]


class TenantLimitsNotVisible(LookupError):
    """The tenant row is not visible to this session (wrong tenant context). Fails closed."""


@dataclass(frozen=True)
class DocumentLimits:
    max_file_bytes: int
    max_pdf_pages: int
    max_pptx_slides: int
    max_documents: int
    max_storage_bytes: int
    max_pages: int
    uploads_per_user_per_hour: int
    uploads_per_tenant_per_hour: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _check_value(key: str, value: Any) -> int:
    if key not in PLATFORM_CEILINGS:
        raise DocumentLimitsError("unknown_limit", key)
    # bool is a subclass of int in Python and must not pass as a number
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise DocumentLimitsError("invalid_limit_value", key)
    if value > PLATFORM_CEILINGS[key]:
        raise DocumentLimitsError("limit_above_ceiling", key)
    return value


def validate_overrides(overrides: Mapping[str, Any]) -> dict[str, int]:
    """A clean copy of the overrides, or DocumentLimitsError for the first bad key or value."""
    return {key: _check_value(key, value) for key, value in overrides.items()}


def default_limits() -> DocumentLimits:
    """The platform defaults from the environment, checked against the ceilings."""
    values = {key: _check_value(key, getattr(settings, _SETTING_FOR_KEY[key])) for key in LIMIT_KEYS}
    return DocumentLimits(**values)


def merge_limits(overrides: Mapping[str, Any] | None) -> DocumentLimits:
    """The effective limits: defaults with the (validated) tenant overrides on top."""
    values = default_limits().as_dict()
    values.update(validate_overrides(overrides or {}))
    return DocumentLimits(**values)


def apply_patch(current: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, int]:
    """
    New overrides after a PATCH: a value sets the override, null removes it (back to the default).
    Every key of the patch must be known; the result is validated as a whole. `current` is not changed.
    """
    result = dict(current)
    for key, value in patch.items():
        if key not in PLATFORM_CEILINGS:
            raise DocumentLimitsError("unknown_limit", key)
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return validate_overrides(result)


async def load_tenant_limits(db: AsyncSession, tenant_id: uuid.UUID) -> DocumentLimits:
    """
    The tenant's effective limits, read through the caller's tenant session. A row that is not
    visible (another tenant, no context) raises instead of falling back to the defaults.
    """
    row = (await db.execute(select(Tenant.doc_limits).where(Tenant.id == tenant_id))).one_or_none()
    if row is None:
        raise TenantLimitsNotVisible()
    stored = row[0]
    if not isinstance(stored, dict):
        raise DocumentLimitsError("invalid_limit_value", "doc_limits")
    return merge_limits(stored)
