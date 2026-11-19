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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


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


class PublishRequest(_Strict):
    """POST .../versions/{version_id}/publish. `acknowledge_flags` must be a real boolean (no "yes", no null)."""

    acknowledge_flags: StrictBool = False


class TenantDocumentLimitsResponse(_Strict):
    """Superadmin view of one tenant's limits."""

    tenant_id: uuid.UUID
    overrides: dict[str, int]
    effective: DocumentLimitsModel
    ceilings: DocumentLimitsModel


# ── Test search and flags (step 5d-2) ────────────────────────────────────────
# The models are named after the endpoint; __test__ = False stops pytest from collecting them as tests.

# The raw query string is capped here (body hygiene: the standard FastAPI 422). The contract limits, 1 to
# RETRIEVAL_MAX_QUERY_CHARS (500) characters AFTER cleaning, are checked by the route with the stable codes
# invalid_query and query_too_long, after the scope and the rate limit.
TEST_SEARCH_RAW_QUERY_MAX = 4000
TEST_SEARCH_MAX_K = 10


class TestSearchRequest(_Strict):
    """POST /api/v1/documents/test-search. The ids only NARROW the caller's own (class, subject) pairs."""

    __test__ = False

    query: StrictStr = Field(max_length=TEST_SEARCH_RAW_QUERY_MAX)
    class_id: uuid.UUID | None = None
    subject_id: uuid.UUID | None = None
    include_drafts: StrictBool = False
    k: StrictInt = Field(default=6, ge=1, le=TEST_SEARCH_MAX_K)


class TestSearchHit(_Strict):
    """One ranked chunk. document_title, heading_path and content are UNTRUSTED text from an uploaded file:
    show them as text, never as markup. `flagged` / `injection_flags` cover the chunk text only (heading path
    and content, scanned at ingest); title_flagged and filename_flagged are computed for this response only.
    None of them = false means safe."""

    __test__ = False

    rank: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    version_id: uuid.UUID
    version_no: int
    version_status: str
    page_start: int | None
    page_end: int | None
    heading_path: str | None
    content: str
    vector_rank: int | None
    vector_similarity: float | None
    keyword_rank: int | None
    keyword_matched_terms: int | None
    fused_score: float
    flagged: bool
    injection_flags: list[str]
    title_flagged: bool  # the CURRENT title, scanned when this response is built
    filename_flagged: bool  # the version's original file name, scanned when this response is built


class TestSearchResponse(_Strict):
    __test__ = False

    sufficient: bool
    reason: str
    message: str | None
    threshold: float
    threshold_calibrated: bool
    keyword_coverage_threshold: float | None
    model: str
    candidate_count: int
    hits: list[TestSearchHit]


class FlagMatch(_Strict):
    """Where one code matched in a chunk: `source` is heading_path or content, `excerpt` at most 300 characters
    of that UNTRUSTED text around the matched span. Show it as text, never as markup."""

    code: str
    source: Literal["heading_path", "content"]
    excerpt: str


class FlaggedChunk(_Strict):
    """A flagged chunk of a version. `excerpt` is at most 300 characters of the chunk's UNTRUSTED content,
    around the first match in the content (or its start); `matches` has one entry per code the CURRENT rules
    find, with its source and span (a stored code that no longer matches has none; see rescan_flags). Show all
    of it as text, never as markup."""

    chunk_id: uuid.UUID
    ordinal: int
    page_start: int | None
    page_end: int | None
    heading_path: str | None
    injection_flags: list[str]
    excluded: bool
    excerpt: str
    matches: list[FlagMatch]


class VersionFlagsResponse(_Strict):
    """GET .../versions/{version_id}/flags: what a teacher acknowledges before publishing."""

    document_id: uuid.UUID
    version_id: uuid.UUID
    version_no: int
    version_status: str
    flagged_chunk_count: int | None
    flags_acknowledged: bool
    title_flagged: bool
    filename_flagged: bool
    items: list[FlaggedChunk]
    next_after_ordinal: int | None  # pass it back as after_ordinal for the next page; null on the last page
