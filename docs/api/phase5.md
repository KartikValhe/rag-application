# Phase 5 API Reference: Documents

Started during the 5a-2 security hardening. This page lists the contract changes to existing endpoints (section 1), the stable error codes (section 2), the upload rules built in step 5a-3 (section 3) and the document routes built in step 5b-1 (sections 4-6). Publish, flags and test search come in 5c/5d; until a worker exists (5b-2) uploaded versions stay `queued`.

---

## 1. Changes to existing endpoints

### 1.1 `DELETE /api/v1/subjects/{subject_id}`

Permission `subject:manage` (unchanged). A subject that still has documents can no longer be deleted: before migration 0009 the delete silently cascaded to the subject's documents, versions, chunks and embeddings and left their files in the object store.

- **Success:** `200` `{"message": "Subject deleted successfully."}` (unchanged).
- **`404 not_found`:** the subject does not exist in the tenant, or a teacher is not assigned to it (unchanged, generic).
- **`409 Conflict`, code `subject_has_documents`:**

  ```json
  {"detail": {"code": "subject_has_documents", "message": "This subject still has documents, including deleted ones that are not purged yet. Remove them before deleting the subject."}}
  ```

  - Soft-deleted documents (tombstones) count until the sweeper purges them (30 days) or an admin purges them (`POST /api/v1/admin/documents/{document_id}/purge`).
  - Nothing is deleted when the 409 is returned.
  - The database enforces the same rule (`fk_documents_subject` is `ON DELETE RESTRICT`), so a document created between the check and the delete also ends in this 409.

## 2. New stable error codes

| Code | HTTP | Where | Meaning |
|---|---|---|---|
| `subject_has_documents` | 409 | `DELETE /api/v1/subjects/{subject_id}` | The subject still has documents (live or not yet purged). The frontend error mapper is a later task (backlog B20). |
| `duplicate_version` | 409 | document upload and replace (5b) | The same bytes already exist as an **active** version (`queued`, `processing`, `ready`, `published`) of this document. Bytes of a `failed` or `archived` version can be uploaded again. |
| `unsupported_format` | 422 | document upload (5b) | The content is a valid file of a type documents do not accept: **XLSX**, or text with a `.csv` extension. Documents accept PDF, DOCX, PPTX, TXT and MD. |
| `invalid_encoding` | 422 | document upload (5b) | A text file (TXT, MD) is not UTF-8. A UTF-8 BOM is allowed. |
| `too_many_pages` | 422 | document upload (5b) | A PDF has more pages than the tenant's page limit (best-effort scan of uncompressed page objects; the parser checks again, design A3). |
| `too_many_slides` | 422 | document upload (5b) | A PPTX has more slides than the tenant's slide limit. Slides are the `p:sldId` entries of `ppt/presentation.xml`, whatever the slide parts are called. |
| `xml_too_complex` | 422 | document upload and timetable import | An XML part of the package nests elements more than 256 levels deep. |
| `file_too_large` | 413 | document upload | The body or file exceeds a limit: the middleware ceiling (50 MB + 64 KB body), the tenant's own file limit (default 10 MB), or the platform ceiling of 50 MB. |
| `too_many_parts` | 413 | document upload | More than 6 multipart parts. |
| `duplicate_header` | 400 | **every route** (middleware) | `Content-Type` or `Content-Length` appears more than once. Refused before the body is read. |
| `invalid_content_length` | 400 | **every route** (middleware) | `Content-Length` is not a plain non-negative decimal integer (at most 18 digits). |
| `missing_token` / `invalid_token` | 401 | every upload route (middleware) | No `Authorization: Bearer` tenant access token, or its signature, type or expiry does not verify. Sent with `WWW-Authenticate: Bearer`, before the body is read. |
| `length_required` | 411 | every upload route with a body (middleware) | No `Content-Length` header (chunked uploads are refused). |
| `unsupported_media_type` | 415 | every multipart upload route (middleware) | The body is not `multipart/form-data` with a non-empty boundary. |
| `too_many_concurrent_uploads` | 429 | every upload route with a body (middleware) | Too many uploads in flight from one client IP (default 4). Sent with `Retry-After: 5`. |
| `service_unavailable` | 503 | every upload route with a body (middleware) | Redis (per-IP cap) unreachable outside development: fail closed. |
| `pdf_active_content` | 422 | document upload **and timetable import** | PDF contains `/JavaScript`, `/JS`, `/Launch`, `/RichMedia` or `/XFA` (also written with `#xx` escapes). `/OpenAction` and `/AA` are **allowed**. Fixed message: "PDFs with scripts, launch actions or rich media are not allowed." |
| `pdf_embedded_file` | 422 | document upload **and timetable import** | PDF contains the name `/EmbeddedFile`, which means another file is attached inside it (also written with `#xx` escapes). Added 2026-11-18; before that this case returned `pdf_active_content`. If a PDF has both kinds, `pdf_active_content` wins. The plural `/EmbeddedFiles` (the catalog's name tree key) and a file specification without a stream are **allowed**. Fixed message, safe to show to the teacher as is: "This PDF has another file attached inside it (some tools add one when you export or sign a PDF). Export or print it again without attachments, or upload a DOCX version instead." In the document flow it ends the job for good (the version becomes `failed` with this code as its `error_code`, not retryable). |
| `pdf_too_complex` | 422 | document upload and timetable import | PDF has more than 20,000 objects (timetable: also more than 100 pages). |

All upload errors keep the Phase 4 shape `{"detail": {"code", "message"}}` with a **fixed message per code** (no file names, member names, paths or library text). The other codes a document upload can return (`empty_file`, `disallowed_file_type`, `file_type_mismatch`, `invalid_openxml_format` (also: an unsafe zip entry name, or two entry names that collide after NFC + casefold), `macro_enabled_file`, `embedded_content`, `external_relationship`, `invalid_xml`, `xml_entity_expansion_forbidden`, `corrupt_archive`, `encrypted_file`, `zip_bomb_detected`, `zip_member_too_large`) are the Phase 4 codes with the same meaning.

## 3. Upload rules for documents (step 5a-3)

Implemented in `app/core/upload_security.py` (`UploadProfile.DOCUMENT`, `validate_upload`), `app/core/body_limit.py` and `app/services/document_store.py`. The routes that use them come in 5b.

### 3.1 Request guard and per-route body limits (middleware, before the body is read)

`BodyLimitMiddleware` runs directly inside CORS and before every other layer; forms are only parsed inside the routes, so all of this happens before FastAPI reads (or spools) a body. Changed by the security review of 5a-3.

**Every request:** a repeated `Content-Type` or `Content-Length` gives `400 duplicate_header` (Starlette reads the first value, so two values could make the check and the parser disagree); a `Content-Length` that is not a plain non-negative integer gives `400 invalid_content_length`.

**Upload routes** are a registry keyed by **method + full-path regex** (`UPLOAD_ROUTES` in `app/core/body_limit.py`), checked in this order:

1. `Authorization: Bearer` with a tenant access token whose signature, type and expiry verify (no DB access, no tenant lookup), else `401`. The route's own authorization (permission, scope, tenant match, user status) is unchanged and still returns its usual codes, including the generic 404.
2. Multipart routes: `multipart/form-data` with a boundary, parsed with python-multipart exactly as Starlette does, else `415 unsupported_media_type`. Routes with a body: `Content-Length` present, else `411 length_required`, and not above the limit, else `413 file_too_large`.
3. Per-IP in-flight cap (`SlotManager`, key `concurrency:upload:ip:{ip}`, 4 at a time, TTL 120 s, released when the response ends; the client IP follows the `TRUST_PROXY` rule): `429 too_many_concurrent_uploads` with `Retry-After`. Redis unreachable outside development: `503 service_unavailable`.

| Method and path | Kind | Body limit | Parts |
|---|---|---|---|
| `POST /api/v1/documents`, `POST /api/v1/documents/{uuid}/versions` (5b) | multipart | 50 MB + 64 KB | 6 |
| `POST /api/v1/admin/branding/logo` and `/favicon`, `POST /api/v1/admin/users/import`, `POST /api/v1/classes/{id}/students/import`, `POST /api/v1/classes/{id}/timetables/import-preview` | multipart | 2 MB + 64 KB | 5 |
| `POST /api/v1/classes/{id}/timetables/save-imported-draft` | JSON (steps 1-3, no content-type check) | 2 MB + 64 KB | - |
| `GET /api/v1/classes/{id}/timetables/template.csv` and `.xlsx` | download (step 1 only) | 2 MB + 64 KB | - |
| **any other** request under `/api/v1/documents` (other methods, other paths, any content type) | - | **64 KB** | 5 |
| any other multipart request | - | 2 MB + 64 KB | 5 |

Over the limit the answer is `413` with `file_too_large` (or `too_many_parts`). The bytes are also counted while they stream, so a body longer than its `Content-Length` is cut off at the limit. The middleware cannot know the tenant, so a tenant's own limit (default 10 MB) is enforced by the route while it streams the file. A file of exactly 50 MB fits the documents limit; the platform ceiling `DOC_UPLOAD_CEILING_BYTES` (50 MB) is also a hard cap inside `stream_upload_with_limit`. A document id that is not a canonical UUID does not match the versions route and falls under the 64 KB rule (fails closed). Every route that parses a form must be in the registry; `tests/test_upload_preauth.py` checks this against the app's OpenAPI schema.

### 3.2 Accepted formats and caps (`validate_upload`, `UploadProfile.DOCUMENT`)

- **Formats:** PDF, DOCX, PPTX, TXT, MD. The type comes from the content; the extension must agree (`file_type_mismatch` otherwise). Text must be UTF-8 (BOM allowed, else `invalid_encoding`); `.md` or `.txt` is decided by the extension once the content is known to be text; any other extension is `file_type_mismatch`. CSV and XLSX give `unsupported_format`.
- **Per-file size:** a parameter (the tenant's `max_file_bytes`), never above 50 MB.
- **Zip caps (OpenXML):** 60 MB uncompressed in total, 25 MB per member, compression ratio 100, at most 1,000 members (timetable imports keep 15 MB, 5 MB, 50 and 200).
- **OpenXML rules** (same as Phase 4): no macros, no `embeddings/`, no nested zip or OLE inside the package, no external relationships except plain hyperlinks (never fetched), XXE and entity expansion blocked, every XML check fails closed. Added by the second review of 5a-3, for DOCX, PPTX and XLSX in **both** profiles: every zip entry is checked (an absolute name, a backslash, `..`, `:`, NUL or a control character, or two names equal after NFC + casefold, is `invalid_openxml_format`); `activeX/` parts and an `aFChunk` (altChunk) relationship are `embedded_content`; every `.xml` part under `word/` and `xl/` must parse (`invalid_xml` / `xml_entity_expansion_forbidden`). XML is streamed (no tree) with a nesting cap of 256 (`xml_too_complex`), so memory does not grow with the part. For PPTX in the document profile the rules also cover the `ppt/` parts: every XML part under `ppt/` must parse.
- **Page and slide caps** are parameters: PDF pages (default 200, `too_many_pages`), PPTX slides (default 100, `too_many_slides`, counted as `p:sldId` entries). The PDF page count is a best-effort scan of uncompressed bytes; the page count must be checked again after parsing in 5b.

### 3.3 The PDF rule (documents and timetable imports)

Rejected with `pdf_active_content`: `/JavaScript`, `/JS`, `/Launch`, `/RichMedia`, `/XFA`; rejected with `pdf_embedded_file`: `/EmbeddedFile` (a real attached file; the singular name only, `/EmbeddedFiles` is allowed). A name ends at whitespace (including NUL) or one of `()<>[]{}/%`. Both kinds are also caught with `#`-escaped spellings such as `/J#61vaScript`. **Allowed:** `/OpenAction` and `/AA`. The object and page caps stay. Every regex of the scan uses bounded repetition only (for example `[0-9]{1,10}\s{1,10}[0-9]{1,5}\s{1,10}obj\b`), so the scan is linear in the file size; before the 5a-3 review 2 MB of digits behind a `%PDF-` header kept it busy for hours. An object or page written with unusual amounts of whitespace is simply not counted. Routes call `validate_upload_async`, which runs the validation in a worker thread for both profiles; a thread cannot be killed, so the linear algorithms are the protection and the thread only keeps the event loop responsive. The scan reads **uncompressed** bytes only: a name or a page object inside a compressed object stream is invisible to it, so it cannot be relied on to find active content or to count pages. It is a first filter. The sandboxed document parser arrives in 5b (its memory limit applies on Linux only) and must re-check the page count after parsing (backlog B33). The scan works in 1 MB slices, so its memory does not grow with the file.

### 3.4 Private document store

Originals are kept in a **private** store (`get_document_store()`), separate from the public branding store: its own root (`DOCUMENT_STORE_DIR`), no URL method, never served by a route. Keys are `t/{tenant_id}/d/{32 random hex}`; every call names the caller's tenant and any key outside that tenant's prefix (or in another shape) raises `InvalidStorageKey`. Writes are atomic (temp file in the same directory with `O_EXCL` and mode 0600, `fsync`, then `os.replace`; a failed or interrupted write leaves no file); directories are 0700 (best effort on Windows, which ignores POSIX modes). `stream()` opens the file only when iteration starts and closes it in `finally`. The public branding store's `get_url` refuses a document key. Downloads are streamed through an authenticated route (section 4). Supabase Storage replaces the local backend later behind the same interface.

---

## 4. Document routes (step 5b-1)

Implemented in `app/api/v1/documents.py`; scope in `app/services/document_scope.py`; models in `app/schemas/document.py`. Every route is behind `require(permission)` (default-deny). Every error is `{"detail": {"code", "message"}}` with a fixed message per code (a few add fields, noted below). Students hold no `document:*` permission, so every document route answers them `403 permission_denied`.

**Scope rule (one shared function).** Scope is a set of (class, subject) **pairs**, never a cross product. Admins: every offered pair. Teachers: their `teacher_assignments` pairs that the class offers. Anyone else (including a custom role with only `document:view`): no pairs.
- *View* needs one linked pair in scope. *Manage* (replace, rename, unlink, delete, retry, version delete) needs **all** linked pairs.
- No linked pair in scope, unknown id, another tenant's id or a soft-deleted document: **`404 not_found`**, always the same body `{"detail": {"code": "not_found", "message": "Resource not found."}}`.
- Some but not all pairs: **`403 document_manage_requires_all_classes`**. `DocumentResponse.can_manage` is then `false` and `blocked_reason` is `"document_manage_requires_all_classes"`, so the UI can explain it.

### 4.1 Routes

`...` below is `/api/v1/documents/{document_id}`. A version id that belongs to another document is the generic 404.

| Method and path | Permission | Request | Success | Errors (besides 401 and 403 `permission_denied`) |
|---|---|---|---|---|
| `POST /api/v1/documents` | `document:upload` | multipart: `file`, `title` (1-200 characters after trimming), `subject_id`, `class_ids` (repeat the field; 1-50) | `202` `DocumentVersionResponse` | 404; 413; 422 `invalid_title`, `too_many_classes`, validation and quota codes; 429; 503 |
| `POST .../versions` | `document:upload` + manage on all classes | multipart: `file` | `202` `DocumentVersionResponse` | 403 manage; 404; 409 `duplicate_version`, `document_processing_in_progress`; 413; 422; 429; 503 |
| `GET /api/v1/documents` | `document:view` | query: `class_id`, `subject_id`, `status` (of the latest version), `cursor`, `limit` (1-100, default 20) | `200` `DocumentListResponse` | 422 `invalid_cursor`, `invalid_status` |
| `GET ...` | `document:view` | | `200` `DocumentResponse` | 404 |
| `PATCH ...` | `document:upload` | JSON `DocumentUpdateRequest` | `200` `DocumentResponse` | 403 manage; 404; 422 (`invalid_title`, an unknown field such as `subject_id`, empty `class_ids`) |
| `DELETE ...` | `document:delete` + manage on all classes | | `204` | 403 manage; 404 |
| `GET .../versions/{version_id}/status` | `document:view` | | `200` `DocumentStatusResponse` | 404 |
| `POST .../versions/{version_id}/retry` | `document:upload` + manage on all classes | | `202` `DocumentVersionResponse` | 403 manage; 404; 409 `version_not_retryable`, `document_processing_in_progress`, `duplicate_version`, `retry_limit_reached`; 429 `retry_rate_limit_exceeded`, `retry_version_rate_limit_exceeded`; 503 |
| `POST .../versions/{version_id}/publish` | `document:publish` + manage on all classes | JSON `PublishRequest` `{acknowledge_flags?: boolean}` (the body may be omitted) | `200` `DocumentVersionResponse` | 403 manage; 404; 409 `version_not_ready`, `flags_acknowledgement_required`, `publish_conflict`; 422 (a non-boolean `acknowledge_flags`, an unknown key) |
| `DELETE .../versions/{version_id}` | `document:upload` + manage on all classes | | `204` | 403 manage; 404; 409 `version_not_deletable` |
| `GET .../versions/{version_id}/download` | `document:view` | | `200` file stream | 404 (also when the file is missing) |
| `GET .../versions/{version_id}/flags` (5d-2) | `document:view` | query: `after_ordinal` (-1 or more), `limit` (1-200, default 50) | `200` `VersionFlagsResponse` | 404; 422 (paging values) |
| `POST /api/v1/documents/test-search` (5d-2) | `document:view` | JSON `TestSearchRequest` | `200` `TestSearchResponse` | 404; 422 `invalid_query`, `query_too_long` and schema errors; 429 `test_search_rate_limit_exceeded`; 503 `service_unavailable`, `search_unavailable`, `search_timeout` |
| `GET /api/v1/admin/documents/usage` | `settings:manage` | | `200` `DocumentUsageResponse` | |
| `POST /api/v1/admin/documents/{document_id}/purge` | `settings:manage` | | `204` | 404; 409 `document_not_deleted` |
| `PATCH /api/v1/platform/tenants/{tenant_id}/limits` | platform superadmin | JSON object of limit overrides (section 6) | `200` `TenantDocumentLimitsResponse` | 401; 404; 422 `unknown_limit`, `invalid_limit_value`, `limit_above_ceiling` |

### 4.2 Behaviour

- **Upload** (both POST routes, design A3): scope check (404), then the hourly rate limits for the user and the tenant (429 with `Retry-After`; Redis down outside development gives 503), then the file is read within the **tenant's** `max_file_bytes` (413), then `validate_upload_async` with the document profile and the tenant's page and slide caps (codes in sections 2 and 3), then sha256. Then one transaction: quota lock, duplicate and in-flight checks, quota check, write to the private store, document, links, version (`queued`), ingest job (`queued`), audit `document:upload` or `document:version_upload`. If anything after the file write fails, the file is deleted again. A rejected upload leaves no file and no rows.
- **`duplicate_version` (409)** carries `version_id`, the active version with the same bytes. Bytes of a `failed` or `archived` version may be uploaded again.
- **`duplicate_content`** is a warning, not an error: `DocumentVersionResponse.warnings` contains it when the same bytes exist in another live document the caller can see. Otherwise `warnings` is `[]`.
- **Quotas (422):** `quota_documents_exceeded` (live documents + 1 > `max_documents`); `quota_storage_exceeded` (bytes of every version of every live document + the new file > `max_storage_bytes`); `quota_pages_exceeded` (pages of active versions already at `max_pages`: the page count of a new file is only known after parsing, which checks again). A value exactly at the limit is allowed. Soft-deleted documents do not count.
- **PATCH:** linking a class needs that (class, subject) pair in the caller's own pairs (else 404); renaming or unlinking needs manage on all linked classes (else 403). The subject never changes. Audited as `document:update` with the added and removed class ids.
- **DELETE (soft), one transaction:** tombstone (`deleted_at`, `deleted_by`); chunks deleted; versions `archived`; queued or running jobs `failed` with `last_error_code = "document_deleted"`; every file into the storage outbox (due now); audit `document:delete`. The document then answers 404 to everyone, and the subject cannot be deleted (`409 subject_has_documents`) until the tombstone is purged.
- **Purge:** only for a soft-deleted document. Deletes the tombstone and its versions (chunks, links and jobs cascade) and its files now; a file that cannot be deleted right now stays in the outbox for the sweeper. Audited as `document:purge`.
- **Retry:** only a `failed` version whose `error_code` is transient (`processing_failed`, `worker_crash`). The version goes back to `queued`; the job to `queued` with `attempts = 0`, `run_after = now()` and no error. Audited as `document:retry`.
- **Publish (step 5c-2)**, one transaction: the document row is locked, the version must be `ready` (`409 version_not_ready`) and, when it has flagged passages (`flagged_chunk_count > 0`), the request must carry `acknowledge_flags: true` (`409 flags_acknowledgement_required`; the acknowledging user is stored as `flags_acknowledged_by` and audited as `document:flags_acknowledged`). Then the previous published version becomes `archived` (its chunks and vectors are deleted; its file waits in the storage outbox for `DOC_ARCHIVE_FILE_RETENTION_DAYS`, default 30, from the moment of publishing), the new version becomes `published` with `published_at` and `published_by`, archived versions beyond the newest `DOC_MAX_ARCHIVED_VERSIONS` (default 5) are pruned (rows deleted, files queued for deletion at once; audited as `document:prune_archived`), and `document:publish` is audited (`details.auto = false`; the worker's auto-publish writes `true`). A reader never sees zero or two published versions of a document. A teacher needs an assignment in EVERY linked class (403 `document_manage_requires_all_classes` otherwise, generic 404 out of scope); students and a role with only `document:view` get 403. Concurrent publishes serialise on the document row: the loser gets `409 version_not_ready`. A unique violation on the one-published-version index (it cannot happen while the row lock is held) is the stable `409 publish_conflict`. **Auto-publish is OFF** (`DOC_AUTO_PUBLISH_ENABLED=false`) until the injection scanner of 5d exists; today a version that finishes processing stays `ready` until someone publishes it. Replacing a published document leaves the old version published (and searchable) until the new one is published. **Since 5d-2** the injection scanner sets `flagged_chunk_count` (section 11); auto-publish still defaults to off (`DOC_AUTO_PUBLISH_ENABLED=false`, plan Q5) because the scanner is a tripwire whose false-positive rate on real files is not yet measured.
- **Version delete:** only `ready` (draft) or `failed` versions. The row goes and its file goes into the outbox. Audited as `document:version_delete`.
- **Download headers:** `Content-Disposition: attachment` (built by `attachment_header()` from the stored, sanitized file name), `Content-Type: application/octet-stream`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: sandbox; default-src 'none'; frame-ancestors 'none'`, `Cache-Control: private, no-store`, `Content-Length`. The file is found through the database row; the store refuses any key outside the caller's tenant. A file that vanishes during the download aborts the response. Audited as `document:download`.

## 5. Response and request models

All models have `extra="forbid"`, and **every key is always present** (`null` when empty). Timestamps are ISO 8601 with a time zone.

- `NamedRef`: `{id, name}`.
- `DocumentVersionResponse`: `{id, document_id, version_no, status, stage, error_code, file_kind, original_filename, size_bytes, page_count, chunk_count, flagged_chunk_count, created_at, published_at, warnings: string[]}`. `status` is one of `queued, processing, ready, published, archived, failed`; `file_kind` one of `pdf, docx, pptx, txt, md`.
- `DocumentResponse`: `{id, title, description, subject: NamedRef, classes: NamedRef[], created_by, created_at, updated_at, latest_version: DocumentVersionResponse | null, published_version: DocumentVersionResponse | null, can_manage: boolean, blocked_reason: "document_manage_requires_all_classes" | null}`.
- `DocumentListResponse`: `{items: DocumentResponse[], next_cursor: string | null}`. Pass `next_cursor` back as `cursor` for the next page; it is opaque.
- `DocumentStatusResponse`: `{document_id, version_id, version_no, status, stage, error_code, retryable: boolean, updated_at}`.
- `DocumentUsageResponse`: `{documents, storage_bytes, pages, limits: DocumentLimits}`.
- `DocumentLimits`: `{max_file_bytes, max_pdf_pages, max_pptx_slides, max_documents, max_storage_bytes, max_pages, uploads_per_user_per_hour, uploads_per_tenant_per_hour}`, all integers.
- `PublishRequest` (publish body): `{acknowledge_flags?: boolean}`, default `false`. It must be a real boolean (`"yes"`, `1` and `null` are a 422); any other key is a 422.
- `DocumentUpdateRequest` (PATCH body): `{title?: string (1-200), class_ids?: uuid[] (1-50)}`. Any other key is a 422.
- `TenantDocumentLimitsResponse`: `{tenant_id, overrides: {key: integer}, effective: DocumentLimits, ceilings: DocumentLimits}`.
- `TestSearchRequest` (5d-2): `{query: string, class_id?: uuid, subject_id?: uuid, include_drafts?: boolean (default false), k?: integer 1-10 (default 6)}`. `query` must be a JSON string of at most 4,000 characters (schema); the contract limits (1-500 characters after cleaning) give the stable codes of section 11. `include_drafts` must be a real boolean and `k` a real integer. Any other key (for example `tenant_id`) is a 422.
- `TestSearchResponse` (5d-2): `{sufficient, reason, message: string | null, threshold, threshold_calibrated, keyword_coverage_threshold: number | null, model, candidate_count, hits: TestSearchHit[]}`.
- `TestSearchHit` (5d-2): `{rank, chunk_id, document_id, document_title, version_id, version_no, version_status, page_start, page_end, heading_path, content, vector_rank, vector_similarity, keyword_rank, keyword_matched_terms, fused_score, flagged: boolean, injection_flags: string[], title_flagged: boolean, filename_flagged: boolean}`. Numbers that a side did not produce are `null`.
- `VersionFlagsResponse` (5d-2): `{document_id, version_id, version_no, version_status, flagged_chunk_count: integer | null, flags_acknowledged: boolean, title_flagged: boolean, filename_flagged: boolean, items: FlaggedChunk[], next_after_ordinal: integer | null}`; `FlaggedChunk`: `{chunk_id, ordinal, page_start, page_end, heading_path, injection_flags: string[], excluded: boolean, excerpt: string}`.

## 6. Per-tenant limits (`PATCH /api/v1/platform/tenants/{tenant_id}/limits`)

The body is a JSON object: a value sets an override, `null` removes it (back to the platform default). Overrides merge with the existing ones. Values must be positive integers (not booleans, strings or floats) and at most the platform ceiling. They are stored in `tenants.doc_limits`. The audit entry `tenant:update_doc_limits` goes into the tenant's own audit log with `actor_id = null` and `details = {previous, new, platform_admin_id}`. An unknown tenant is the generic 404.

| Key | Default (env) | Ceiling |
|---|---|---|
| `max_file_bytes` | 10 MB (`DOC_MAX_FILE_BYTES`) | 50 MB |
| `max_pdf_pages` | 200 (`DOC_MAX_PDF_PAGES`) | 2,000 |
| `max_pptx_slides` | 100 (`DOC_MAX_PPTX_SLIDES`) | 1,000 |
| `max_documents` | 200 (`QUOTA_MAX_DOCUMENTS`) | 10,000 |
| `max_storage_bytes` | 500 MB (`QUOTA_MAX_STORAGE_BYTES`) | 20 GB |
| `max_pages` | 5,000 (`QUOTA_MAX_PAGES`) | 200,000 |
| `uploads_per_user_per_hour` | 20 (`DOC_UPLOAD_RATE_USER`) | 500 |
| `uploads_per_tenant_per_hour` | 100 (`DOC_UPLOAD_RATE_TENANT`) | 5,000 |

The API reads a tenant's limits through the tenant session (`app_tenant` may read `doc_limits` of its own row only, migration `0011`). A stored value that fails validation makes the upload fail instead of silently raising a limit.

## 7. New stable codes in 5b-1

| Code | HTTP | Where |
|---|---|---|
| `document_manage_requires_all_classes` | 403 | replace, PATCH (rename or unlink), DELETE, retry, version delete |
| `document_processing_in_progress` | 409 | replace, retry: a version is `queued`, `processing` or `ready` |
| `duplicate_version` | 409 | replace, retry; carries `version_id` |
| `version_not_retryable` | 409 | retry: the version is not failed, or failed for a permanent reason |
| `version_not_deletable` | 409 | version delete: the version is not `ready` or `failed` |
| `document_not_deleted` | 409 | purge of a live document |
| `quota_documents_exceeded`, `quota_storage_exceeded`, `quota_pages_exceeded` | 422 | upload, replace |
| `invalid_title` | 422 | upload, PATCH: blank title |
| `too_many_classes` | 422 | upload: more than 50 classes |
| `invalid_cursor`, `invalid_status` | 422 | list |
| `rate_limit_exceeded`, `tenant_rate_limit_exceeded` | 429 + `Retry-After` | upload, replace; the body also has `retry_after_seconds` |
| `retry_limit_reached` | 409 | retry: this version was already retried by hand `DOC_RETRY_MAX_PER_VERSION` times (default 5, counted from the audit log). The file keeps failing: upload it again as a new version |
| `version_not_ready` | 409 | publish: the version is not `ready` (still processing, failed, already published or archived, or another publish just won) |
| `flags_acknowledgement_required` | 409 | publish: the version has `flagged_chunk_count > 0` and the request did not send `acknowledge_flags: true` |
| `publish_conflict` | 409 | publish: the database refused a second published version of the document (a concurrent publish). Reload and try again |
| `retry_rate_limit_exceeded`, `retry_version_rate_limit_exceeded` | 429 + `Retry-After` | retry: more than `DOC_RETRY_RATE_USER` (10) retries per user per hour, or `DOC_RETRY_RATE_VERSION` (3) per version per hour; the body also has `retry_after_seconds`. Checked after the 404/403 checks, so an unknown id never uses up a window |
| `service_unavailable` | 503 | upload and retry: the private store could not be written, or Redis is down outside development (the rate limits fail closed) |
| `unknown_limit`, `invalid_limit_value`, `limit_above_ceiling` | 422 | platform limits endpoint |

**Security headers.** Every response of the API, whatever its status and whoever produced it (a route, the body guard's 401/411/413, an unhandled error turned into a generic `500 {"detail": {"code": "internal_error", ...}}`, a 304), carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, a `Content-Security-Policy` (`default-src 'none'; frame-ancestors 'none'`) and, outside development, HSTS. A route may keep its own CSP only if it has a plain `sandbox` directive (the document download does); any other route CSP is replaced by the default.

The frontend error mapper for these codes and for the request-guard codes of section 2 (400 `duplicate_header` / `invalid_content_length`, 411, 415, 429, 503) is backlog item B36.

## 8. Worker error codes (5b-2 review fix)

These are values of `error_code` on a version (`DocumentVersionResponse`, `DocumentStatusResponse`), written by the ingestion worker; they are never an HTTP error body.

| Code | Retryable by the API | Meaning |
|---|---|---|
| `job_time_limit_exceeded` | no (`version_not_retryable`) | Processing ran for the maximum job time (`INGEST_MAX_JOB_SECONDS`, default 60 minutes since 5c-2: a 1,000-chunk upload embeds for about 5 minutes and the 2,000,000 character ceiling for about 30) and was stopped. Permanent: a retry would reach the same cap, so the job is not retried automatically either. Upload a smaller or simpler file. |

Unchanged for comparison: `processing_failed` (the transient attempts ran out: retryable) and `worker_crash` (retryable). An outage of Redis or the database at the moment a job starts no longer uses up attempts: the version stays `queued` and is processed when the service is back.

**Attempt budgets (review of 5b-3).** An outage (database, Redis, storage) is retried up to `INGEST_MAX_ATTEMPTS` (3) attempts. A **crashed parser child** (killed by a signal, `SIGXCPU`, an out-of-memory kill, no answer at all: `worker_crash`) is transient too, but counts against the smaller `INGEST_MAX_CRASH_ATTEMPTS` (**2**), so a file that kills its parser cannot loop; the version then fails with `processing_failed` (retryable by hand, rate limited). An exception the child can still catch (a library refusing a hostile attribute, a recursion or memory error) is **not** a crash: it is the permanent `parse_failed`, after ONE attempt. The count is of the job's attempts, so an earlier outage retry also counts toward the crash budget (the conservative direction).

**Chunking, embedding and indexing (5c-2).** The stages after the parser add no new error code. `no_text_found` is also raised by the chunker when nothing is left to chunk (a document with headings only). `quota_pages_exceeded` is checked again, bindingly, inside the index transaction with the page count found by parsing (`page_count` of a PDF or PPTX; 1 for DOCX, TXT and MD): the version ends `failed` and nothing is written. Anything unexpected in these stages (a model or database error) is the transient `worker_crash` or `db_unavailable`: the job is retried within the attempt budgets below, and a version whose attempts run out ends `processing_failed`.

**Statuses written by the worker.** A version moves `queued` -> `processing` (with `stage` `parsing`, `chunking`, `embedding` or `indexing`) -> `ready` (the index transaction sets `page_count`, `chunk_count`, `embedding_model` and `flagged_chunk_count`, the number of chunks the injection scanner flagged, section 11) and the job to `done`, all in one transaction. A worker killed at any point before that commit leaves no chunk and no vector, and the next worker produces exactly one set.

## 9. Parser error codes and quality gates (5b-3)

Values of `error_code` on a version (`DocumentVersionResponse`, `DocumentStatusResponse`) written by the ingestion worker's parse stage; never an HTTP error body. Every code is **permanent** (the version is `failed`, `POST .../retry` answers `409 version_not_retryable`) and has one fixed message; no file name, path, library text or document content ever appears. Upload a corrected file as a new version instead.

| Code | Message (fixed) | When |
|---|---|---|
| `scanned_pdf` | The PDF looks scanned: it has almost no selectable text. | Fewer than 20 non-space characters on 80 % or more of ALL pages (empty pages count). There is no OCR in v1. |
| `unreadable_text` | The text uses a font that cannot be read (garbled characters). | (1) More than 30 % of the letters are Private Use Area code points (U+E000-U+F8FF, **without the Symbol-font range U+F000-U+F0FF**: Word bullets such as U+F0B7 and Symbol-font equation characters are content, counted neither as garbage nor as letters; a legacy font that happens to use that range is therefore not caught), or (2) more than 30 % of the non-space characters are "suspect" Latin-1 characters (U+00A1-U+00FF without § © ® ° ± ² ³ µ ¶ · ¹ « » ¿ ¡ × ÷) **and** the file is Devanagari-targeted (PDF `/Lang`, DOCX `w:lang` or PPTX `lang` naming `hi` or `mr`, or at least one Devanagari character in the text). Exactly 30 % passes. **Not detected:** legacy fonts whose codes are plain ASCII (Kruti Dev style `Hkkjr dk lafo/kku`) look like Latin text and pass this gate (backlog B50). |
| `no_text_found` | No text was found in the document. | Nothing is left after cleaning (DOCX, PPTX, TXT, MD; a PDF without text is `scanned_pdf`). |
| `text_too_large` | The document contains more text than can be processed. | More than `DOC_MAX_TEXT_CHARS` (2,000,000) characters after cleaning, more than 500,000 characters on one PDF page, more than 100,000 blocks, or more than 50,000 blocks on one page. Exactly at the limit passes. A **single paragraph** whose RAW length is over `DOC_MAX_TEXT_CHARS` is refused at once, before it is cleaned (cleaning only shortens text, so it could never fit), even if most of it is whitespace. |
| `too_many_pages` | The PDF has too many pages to process. | The page count **the parser sees** is above the tenant's `max_pdf_pages` (also when the upload scan could not count the pages, backlog B33). |
| `too_many_slides` | The presentation has too many slides to process. | The slide count the parser sees is above the tenant's `max_pptx_slides`. |
| `corrupt_pdf` | The PDF document could not be read. | PDFium cannot open the file or a page. |
| `password_protected_pdf` | The PDF is password protected. Remove the password and upload it again. | The PDF needs a password. |
| `pdf_too_complex` | The PDF is too large or complex to process. | Also: a page wider or taller than 14,400 points (200 inches). |
| `parse_timeout` | Processing the document took too long. | The child's own soft deadline (80 % of `DOC_PARSE_TIMEOUT_SECONDS`) or the wall clock. |
| `parse_failed` | The document could not be processed. | The library refused a file that passed validation, or the child sent an unknown code. **Review of 5b-3:** also ANY exception the child catches itself (it reports only the exception class name, to the server log), and a numeric DOCX attribute that is not one or two ASCII digits (`w:outlineLvl w:val="²"`, a 5000-digit value): the file is corrupt. **Permanent, one attempt, no retry.** |
| `xml_too_complex` | The document could not be processed. | Also (**review of 5b-3**): a PPTX whose groups are nested more than 32 levels deep. Text in such a group used to be dropped without a trace; the file is refused instead (documented limit: up to 32 nested groups are read). |
| `parse_output_invalid` | The document could not be processed. | The parent refused the child's answer: it broke the contract (types, extra keys, page order, control characters, statistics that do not match) **or the JSON itself**: not UTF-8, a BOM, trailing text, duplicate keys, `NaN`/`Infinity`, a float or an integer of more than 18 digits, nesting deeper than 8 levels, a NUL or a lone surrogate in any string. Seen only with a broken or compromised parser. Permanent. |
| `parse_output_too_large` | The document could not be processed. | The child's answer was over the output size (`DOC_PARSE_MAX_OUTPUT_BYTES`, default 14 MiB; the real maximum is 12.4 MiB), had more than 413,024 structural tokens (commas, braces and brackets outside strings; checked on the raw bytes before anything is built), or broke a hard cap (pages, blocks, characters per block). |
| `quota_pages_exceeded` | Your organisation has reached its page limit. | The parsed page count would take the tenant over `max_pages` (check function and hook exist since 5b-3; enforced from 5c). |

The validation codes of section 2 (`xml_entity_expansion_forbidden`, `file_type_mismatch`, `corrupt_archive` ...) can also appear here, because the parse stage re-runs the upload validation first. **Change in 5b-3, widened in its review:** every part of a DOCX or PPTX that the libraries could load as XML must parse with defusedxml, wherever it lives and whatever its file name is (`docProps/`, `customXml/`, `word/x.dat` ...): a part is scanned when `[Content_Types].xml` declares an XML content type for it (an Override by part name, else a Default by extension), when its name ends in `.xml`, or when its extension is not a known binary media or font type (png, jpg, gif, bmp, tif, wmf, emf, bin, odttf, ttf, mp3, mp4 ...). A part that is not XML at all then fails as `invalid_xml`. A `DOCTYPE` with entities in any of them gives `xml_entity_expansion_forbidden`. The **timetable import applies the same all-parts scan to DOCX and XLSX** (same libraries); a PPTX in the timetable profile keeps the old narrower scan because the importer refuses it later and never loads its parts.

**What the parser extracts** (for the frontend's "why is this text missing" help): PDF text per page; DOCX body paragraphs, headings and tables (a table cell's own paragraphs and nested tables only; not headers, footers, footnotes, comments, text boxes or images); PPTX slide titles, text boxes, tables and speaker notes (hidden slides included); TXT and MD paragraphs and `#` headings (a line over 1,024 characters is never a heading).

**Hidden text** (`stats.hidden_text_chars`): DOCX hidden characters are **counted and NOT extracted**: `w:vanish` or `w:specVanish` (on the run, its character style, its paragraph style or the document defaults), tracked deletions, runs of 1 pt or less (`w:sz` of 2 half-points or fewer, also through styles), and white runs (`FFFFFF`, also through styles) unless something behind them is shaded (the run's `w:shd` or highlight, the paragraph's, the cell's, or any table that uses a table style: white on a dark fill is a header). Known gaps: PDF invisible text (render mode 3, white fill, off-page) is extracted and not counted (backlog B56); style-inherited shading from a conditional table-style row is approximated by "any table style". PPTX hidden slides are extracted AND counted.

## 10. Retrieval service (5d-1, hardened in 5d-1b)

`app/services/retrieval.py` finds and ranks chunks; the test-search route (5d-2, section 11) calls it and the chat layer (Phase 6) will. Plan: `docs/phase5d-plan.md` sections 3-5 and 14.

**Entry points.** `search(db, scope, ...)` serves test search (read or manage scope; scores shown to a teacher). `search_for_chat(db, scope, ...)` is the only entry point for the chat layer: it accepts only a read scope of published versions, and when the result is not sufficient it returns **no hits and no ranked ids**, so nothing reaches a model. A scope comes only from `build_retrieval_scope` (API) or `RetrievalScope.for_operator` (operator scripts); `scope.narrow(class_id, subject_id)` can only shrink it.

**Every text field of a hit is untrusted.** `content`, `heading_path`, `document_title` and `original_filename` were written by an uploader and may contain instructions aimed at a model. The chat layer must treat all four as data, never as instructions. `flagged = false` does **not** mean safe: the injection scanner (5d-2, which scans the content, the heading path, the title and the file name) is a tripwire with certain false negatives.

**Errors of the service** (the test-search route maps them to HTTP, section 11):

| Error | Code | HTTP | When |
|---|---|---|---|
| `QueryError` | `invalid_query` | 422 | Empty after cleaning, or no word at all (`???`); nothing is embedded |
| `QueryError` | `query_too_long` | 422 | Longer than `RETRIEVAL_MAX_QUERY_CHARS` after cleaning |
| `SearchUnavailable` | `search_timeout` | 503 | A statement ran over `RETRIEVAL_STATEMENT_TIMEOUT_MS` (5,000) |
| `SearchUnavailable` | `search_unavailable` | 503 | The query embedder gave a vector of the wrong size, a non-finite value or a zero norm |
| `ValueError` | (bug) | 500 | `k` outside 1..50, or a manage scope passed to `search_for_chat` |

The message of a `SearchUnavailable` is its code alone: no SQL, no parameters, no query text.

## 11. Test search, injection flags and the flags route (5d-2)

Plan: `docs/phase5d-plan.md` sections 6, 7 and 14. No LLM is called anywhere.

### 11.1 `POST /api/v1/documents/test-search`

For tenant admins and teachers (`document:view`); students get `403`, no token `401`. Checks, in this order:

1. **Permission** (403).
2. **Scope:** the caller's own (class, subject) pairs (section 4), narrowed by `class_id` and/or `subject_id`. The ids can only narrow: a class, subject or pair the caller does not teach, an unknown id or another tenant's id is the generic `404 not_found`, the same body as every other document route. The tenant always comes from the token; the request has no tenant field.
3. **Rate limit:** `DOC_TEST_SEARCH_RATE_USER` (60) searches per user per `DOC_TEST_SEARCH_RATE_WINDOW_SECONDS` (600): `429 test_search_rate_limit_exceeded` with `Retry-After` and `retry_after_seconds`. Checked after the scope, so a probe of foreign ids never uses up the window. Redis down outside development: `503 service_unavailable` (fails closed).
4. **Query:** cleaned like ingested text (NFC; control, zero-width and bidi characters dropped; whitespace collapsed). Empty, or no searchable word at all (`???`): `422 invalid_query`. Over `RETRIEVAL_MAX_QUERY_CHARS` (500) after cleaning: `422 query_too_long`. A JSON string with a lone surrogate (`"\ud800"`) is refused by the schema (FastAPI's `422` list, with U+FFFD echoed in place of the surrogate); before 5d-2 that case ended as a generic 500 on every JSON route.
5. **Model:** the process-wide query embedder is loaded on the first test search of the process (seconds; backlog B96), with no database transaction open. A model that cannot load or fails, or a query vector of the wrong size, with a non-finite value or a zero norm: `503 search_unavailable`.
6. **Search** with a manage scope (section 10). A statement over `RETRIEVAL_STATEMENT_TIMEOUT_MS`: `503 search_timeout`.

`include_drafts: true` adds versions in status **`ready`** (never queued, processing, failed or archived); each hit carries `version_status`. Only this route can ask for drafts: a read scope (students, the chat layer) refuses them.

**Hits are returned even when `sufficient` is false.** This is the teacher's view of what the search found; the chat entry point `search_for_chat` returns none in that case. `threshold_calibrated` stays `false` until a measured threshold replaces the default 0.85, and `model` names the embedding model the threshold belongs to.

Every 503 and 422 message is a fixed text: never SQL, a file path, a library message or the query. The query is **never logged and never audited**; the route logs counts and timings only (`hits`, `flagged_hits`, `candidate_count`, `path`, `reason`, `k`, `include_drafts`, `duration_ms`). Body limit: the documents area rule (64 KB). Security headers as on every response.

### 11.2 `GET /api/v1/documents/{document_id}/versions/{version_id}/flags`

Read-only and not audited. Needs `document:view` and the same scope as the other version routes: one linked pair in scope. Anything else, and a version of another document, is the generic 404. The route lists the flagged chunks of the version in ordinal order:

- `chunk_id`, `ordinal`, the pages, `heading_path`, the codes and `excluded`;
- an `excerpt` of at most **300 characters**. It starts 80 characters before the first match, or at the start of the chunk when the match is in the heading.

Page with `after_ordinal` set to the previous page's `next_after_ordinal`. Use it before `POST .../publish` with `acknowledge_flags: true`.

**The excerpt, the heading path and the title are UNTRUSTED text** from the uploaded file and its uploader. Show them as plain text, never as HTML or Markdown, and never feed them to a model as instructions.

### 11.3 Injection codes (`app/services/injection_scan.py`)

The worker's index stage scans every chunk (heading path, a newline, then the content) before its transaction opens, and stores the codes in `chunks.injection_flags`. `flagged_chunk_count` is the number of chunks with at least one code.

- A version with flags cannot be published without `acknowledge_flags: true` (409 `flags_acknowledgement_required`), and it is never auto-published.
- Flagged chunks of a published version **are returned** by retrieval with `flagged: true` and their codes (flag, never drop).
- The **document title and the original file name** are scanned when a response is built (`title_flagged`, `filename_flagged`), because a title can change after ingest. They are not stored, and they do not change `flagged_chunk_count` or the publish rule (backlog B98).

| Code | What it looks for (English, Hindi, Marathi) |
|---|---|
| `instruction_override` | "ignore / disregard / forget all previous instructions", "override your instructions"; "पिछले निर्देशों को अनदेखा करो", "ऊपर के निर्देश भूल जाओ"; "मागील सूचना दुर्लक्ष करा", "वरील सूचना विसरा" (with and without nukta) |
| `role_markup` | chat-template markers such as `<\|im_start\|>`, `[INST]`, `<<SYS>>`, `### system`, a line starting with `system:` or `assistant:` |
| `role_play` | "you are now (a / DAN / ...)", "from now on you will", "act as an AI", "pretend you are", "developer mode", "jailbreak"; "अब से तुम"; "आतापासून तू" |
| `prompt_extraction` | "reveal / show / print / repeat your (system) prompt or instructions", "what are your instructions"; "अपने निर्देश बताओ"; "तुझ्या सूचना सांग" |
| `model_directive` | text addressed to a model ("AI: you must ...", "ChatGPT, always ..."), "if a student asks ..., say ..."; the same in Hindi and Marathi |
| `tool_call` | `<tool_call>`, `<function_call>`, `"function_call":`, `{"name": ..., "arguments": ...}`, `<\|tool\|>` |
| `link_directive` | a URL near an imperative ("visit", "click", "send to"; "खोलो", "भेजो"; "उघडा", "पाठवा"), or a Markdown image (it can send data out when it loads) |
| `encoded_blob` | 200 or more base64 characters in a row with at least one digit, `+` or `/` |

Matching runs on a normalised copy: format characters removed, Unicode tag characters turned into the ASCII they hide, NFKC, case folded. Full-width letters, mathematical letters, no-break spaces and case make no difference. The stored text is never changed.

**Limits.** This is a tripwire, not a guarantee:

- Paraphrases, other languages, text in images, a phrase split across two chunks and anything not foreseen pass **unflagged**. `flagged: false` never means safe.
- Text that is legitimately **about** these things is flagged: a computer-science lesson on prompts, a teacher's note "if a student asks ..., answer ...", or "Visit www.example.org for the worksheet".
- Not detectable per chunk: zero-width and bidi characters (removed at ingest), PDF hidden text (backlog B56), PPTX hidden slides (backlog B53).

| Code | HTTP | Where |
|---|---|---|
| `invalid_query`, `query_too_long` | 422 | test search |
| `test_search_rate_limit_exceeded` | 429 + `Retry-After` | test search; the body also has `retry_after_seconds` |
| `search_unavailable`, `search_timeout` | 503 | test search |
| `service_unavailable` | 503 | test search: Redis is down outside development |
