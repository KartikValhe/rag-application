"""
app/schemas/document.py
───────────────────────
Request and response models of the document API (design A4, step 5b-1).

Every model forbids unknown fields (extra="forbid") and every key is always present in a response
(optional values are sent as null, never left out), so the frontend's Zod mirror can be exact.
One response model per endpoint; see docs/api/phase5.md.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NamedRef(_Strict):
    """A class or subject: id and display name."""

    id: uuid.UUID
    name: str


class DocumentVersionResponse(_Strict):
    """One uploaded file of a document. Returned with 202 by upload, replace and retry."""

    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    status: str
    stage: str | None
    error_code: str | None
    file_kind: str
    original_filename: str
    size_bytes: int
    page_count: int | None
    chunk_count: int | None
    flagged_chunk_count: int | None
    created_at: datetime
    published_at: datetime | None
    # Non-blocking notices, for example "duplicate_content" when the same bytes already exist in
    # another document the caller can see. Always a list (empty when there is nothing to say).
    warnings: list[str]


class DocumentResponse(_Strict):
    """A document as the caller may see it, with whether they may change it."""

    id: uuid.UUID
    title: str
    description: str | None
    subject: NamedRef
    classes: list[NamedRef]
    created_by: uuid.UUID
    created_at: datetime
    updated_at: datetime
    latest_version: DocumentVersionResponse | None
    published_version: DocumentVersionResponse | None
    # False when the caller teaches the subject in some but not all linked classes. The reason is
    # then "document_manage_requires_all_classes" (the UI explains it); null when can_manage is true.
    can_manage: bool
    blocked_reason: str | None


class DocumentListResponse(_Strict):
    items: list[DocumentResponse]
    next_cursor: str | None


class DocumentStatusResponse(_Strict):
    """Polling view of one version."""

    document_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    status: str
    stage: str | None
    error_code: str | None
    retryable: bool
    updated_at: datetime


class DocumentLimitsModel(_Strict):
    max_file_bytes: int
    max_pdf_pages: int
    max_pptx_slides: int
    max_documents: int
    max_storage_bytes: int
    max_pages: int
    uploads_per_user_per_hour: int
    uploads_per_tenant_per_hour: int


class DocumentUsageResponse(_Strict):
    """What the tenant uses against its limits (soft-deleted documents do not count)."""

    documents: int
    storage_bytes: int
    pages: int
    limits: DocumentLimitsModel


class DocumentUpdateRequest(_Strict):
    """PATCH body. The subject of a document never changes (subject_id is refused as unknown)."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    class_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=50)


class TenantDocumentLimitsResponse(_Strict):
    """Superadmin view of one tenant's limits."""

    tenant_id: uuid.UUID
    overrides: dict[str, int]
    effective: DocumentLimitsModel
    ceilings: DocumentLimitsModel
