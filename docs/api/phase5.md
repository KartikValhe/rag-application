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

**Every text field of a hit is untrusted.** `content`, `heading_path`, `document_title` and `original_filename` were written by an uploader and may contain instructions aimed at a model. The chat layer must treat all four as data, never as instructions. `flagged` and `injection_flags` of a hit cover the **chunk text only** (heading path and content, scanned at ingest). The title and the file name are NOT part of them: they are scanned only when the test-search or flags route answers (`title_flagged` and `filename_flagged` exist in those two responses and nowhere else; the chat layer gets no title flag). `flagged = false` does **not** mean safe: the scanner is a tripwire with certain false negatives.

**Errors of the service** (the test-search route maps them to HTTP, section 11):

| Error | Code | HTTP | When |
|---|---|---|---|
| `QueryError` | `invalid_query` | 422 | Empty after cleaning, or no word at all (`???`); nothing is embedded |
| `QueryError` | `query_too_long` | 422 | Longer than `RETRIEVAL_MAX_QUERY_CHARS` after cleaning |
| `SearchUnavailable` | `search_timeout` | 503 | A statement ran over `RETRIEVAL_STATEMENT_TIMEOUT_MS` (5,000) |
| `SearchUnavailable` | `search_unavailable` | 503 | The query embedder gave a vector of the wrong size, a non-finite value or a zero norm |
| `SearchUnavailable` | `search_busy` | 503 | The query-embedding cap (`RETRIEVAL_EMBED_CONCURRENCY`, 2 per API process) stayed full for `RETRIEVAL_EMBED_QUEUE_SECONDS` (5) |
| `ValueError` | (bug) | 500 | `k` outside 1..50, or a manage scope passed to `search_for_chat` |

The message of a `SearchUnavailable` is its code alone: no SQL, no parameters, no query text.

**Embedding the query (5d-2b).** `retrieval.embed_query(query_embedder, query)` embeds under the per-process cap (an asyncio semaphore of `RETRIEVAL_EMBED_CONCURRENCY` and a thread pool of the same size, not asyncio's shared default pool) and returns a query embedder that answers with that vector for that query only. Call it with no transaction open, then open the search transaction. A caller over the cap waits up to `RETRIEVAL_EMBED_QUEUE_SECONDS`, then gets `search_busy`. The cap is per API process (backlog B103).

## 11. Test search, injection flags and the flags route (5d-2)

Plan: `docs/phase5d-plan.md` sections 6, 7 and 14. No LLM is called anywhere.

### 11.1 `POST /api/v1/documents/test-search`

For tenant admins and teachers (`document:view`); students get `403`, no token `401`. Checks, in this order:

1. **Permission** (403).
2. **Scope:** the caller's own (class, subject) pairs (section 4), narrowed by `class_id` and/or `subject_id`. The ids can only narrow: a class, subject or pair the caller does not teach, an unknown id or another tenant's id is the generic `404 not_found`, the same body as every other document route. The tenant always comes from the token; the request has no tenant field.
3. **Rate limit:** `DOC_TEST_SEARCH_RATE_USER` (60) searches per user per `DOC_TEST_SEARCH_RATE_WINDOW_SECONDS` (600): `429 test_search_rate_limit_exceeded` with `Retry-After` and `retry_after_seconds`. Checked after the scope, so a probe of foreign ids never uses up the window. Redis down outside development: `503 service_unavailable` (fails closed).
4. **Query:** cleaned like ingested text (NFC; control, zero-width and bidi characters dropped; whitespace collapsed). Empty, or no searchable word at all (`???`): `422 invalid_query`. Over `RETRIEVAL_MAX_QUERY_CHARS` (500) after cleaning: `422 query_too_long`. These come AFTER the rate limit, so an unusable query also uses up the window (tested). A JSON string with a lone surrogate (`"\ud800"`) is refused by the schema (FastAPI's `422` list, section 11.4); before 5d-2 that case ended as a generic 500 on every JSON route.
5. **Model:** the process-wide query embedder is loaded on the first test search of the process (seconds; backlog B96). A model that cannot load or fails, or a query vector of the wrong size, with a non-finite value or a zero norm: `503 search_unavailable`.
6. **Embedding** under the per-process cap (section 10): at most `RETRIEVAL_EMBED_CONCURRENCY` (2) query embeddings at once; a search over the cap waits up to `RETRIEVAL_EMBED_QUEUE_SECONDS` (5) and then gets `503 search_busy`. An empty scope embeds nothing.
7. **Search** with a manage scope (section 10), in a second transaction opened only after the embedding. A statement over `RETRIEVAL_STATEMENT_TIMEOUT_MS`: `503 search_timeout`.

The route's own two transactions (steps 2-4 and step 7) are closed while the model loads and embeds. The permission check's session stays open for the whole request (backlog B100).

**Accepted gap: scope and search are two transactions.** The scope (the caller's pairs) is read in the first transaction and used by the search in the second, after the embedding. An assignment revoked in between does not affect the ONE request that already passed its scope check: it still shows that pair's material, as a request that finished a moment earlier would have. The next request is refused (tested: `test_an_assignment_revoked_between_the_scope_and_the_search`). Closing the gap would mean embedding inside the scope transaction, which holds a connection during the model call (the problem of B96).

`include_drafts: true` adds versions in status **`ready`** (never queued, processing, failed or archived); each hit carries `version_status`. Only this route can ask for drafts: a read scope (students, the chat layer) refuses them.

**Hits are returned even when `sufficient` is false.** This is the teacher's view of what the search found; the chat entry point `search_for_chat` returns none in that case. `threshold_calibrated` stays `false` until a measured threshold replaces the default 0.85, and `model` names the embedding model the threshold belongs to.

Every 503 and 422 message is a fixed text: never SQL, a file path, a library message or the query. The query is **never logged and never audited**; the route logs counts and timings only (`hits`, `flagged_hits`, `candidate_count`, `path`, `reason`, `k`, `include_drafts`, `duration_ms`). Body limit: the documents area rule (64 KB). Security headers as on every response.

### 11.2 `GET /api/v1/documents/{document_id}/versions/{version_id}/flags`

Read-only and not audited. Needs `document:view` and the same scope as the other version routes: one linked pair in scope. Anything else, and a version of another document, is the generic 404. The route lists the flagged chunks of the version in ordinal order:

- `chunk_id`, `ordinal`, the pages, `heading_path`, the stored codes (`injection_flags`) and `excluded`;
- an `excerpt` of the content, at most **300 characters**: around the first match in the content, or the content's start when nothing in the content matches;
- `matches` (5d-2b): one entry per code the CURRENT rules find in the chunk, with `source` (`heading_path` or `content`: where the match starts) and `excerpt`, at most **300 characters** around the matched span (up to 80 characters before it). A chunk flagged only because of its heading shows the heading span. A stored code that the current rules no longer find has no entry (run `rescan_flags`, section 11.3).

`flagged_chunk_count: null` means the version was **never scanned** (indexed before 5d-2); `0` means scanned, nothing found.

Page with `after_ordinal` set to the previous page's `next_after_ordinal`. Use it before `POST .../publish` with `acknowledge_flags: true`.

**The excerpts (`excerpt` and every `matches[].excerpt`), the heading path and the title are UNTRUSTED text** from the uploaded file and its uploader. Show them as plain text, never as HTML or Markdown, and never feed them to a model as instructions.

### 11.3 Injection codes (`app/services/injection_scan.py`)

The worker's index stage scans every chunk (heading path, a newline, then the content) before its transaction opens, and stores the codes in `chunks.injection_flags`. `flagged_chunk_count` is the number of chunks with at least one code.

- A version with flags cannot be published without `acknowledge_flags: true` (409 `flags_acknowledgement_required` with `reason: "flagged"`), and it is never auto-published.
- **A version never scanned** (`flagged_chunk_count` NULL: indexed before 5d-2) is treated the same way: "not scanned" is not "clean". Publishing it needs `acknowledge_flags: true` (409 with `reason: "not_scanned"`); the acknowledgement is recorded and audited (`document:flags_acknowledged` with `not_scanned: true`). Test search works on it like on any ready version.
- Flagged chunks of a published version **are returned** by retrieval with `flagged: true` and their codes (flag, never drop). These flags cover the **chunk text only** (heading path and content).
- The **document title and the original file name** are cleaned like chunk text (`clean_text`) and scanned only when the test-search or flags response is built (`title_flagged`, `filename_flagged`, in those two responses only), because a title can change after ingest. They are not stored, they are not part of a hit's `flagged`, and they do not change `flagged_chunk_count` or the publish rule. **A title edited after publish is not reviewed again** (backlog B98).
- **Rescan:** `python -m app.scripts.rescan_flags <tenant slug>` (dry run) or `... --apply` scans the stored chunks of the tenant's ready and published versions with the current rules and writes the codes and `flagged_chunk_count` (one transaction per version, audited as `document:flags_rescanned`; prints counts and ids only). **Versions indexed before 5d-2 need it** (until then publishing them needs an acknowledgement); so does every version after a rule change (backlog B102).

| Code | What it looks for (English, Hindi, Marathi) |
|---|---|
| `instruction_override` | "ignore / disregard / forget (up to five words) previous / prior / above instructions / prompts / directives", "forget your rules", "override your instructions"; "पिछले निर्देशों को अनदेखा करो", "ऊपर के निर्देश भूल जाओ"; "मागील सूचना दुर्लक्ष करा", "वरील सूचना विसरा" (with and without nukta). Not: "forget the old rules of spelling" (an instruction or prompt noun is required) |
| `role_markup` | chat-template tokens such as `<\|im_start\|>` (also spaced: `< \|im_start\| >`), `[INST]`, `<<SYS>>`, `### system`, `### Instruction:`, a line starting with `system:`. Not: `Assistant:` or `User:` speaker labels, `## User guide` |
| `role_play` | "you are / you're now", "pretend you are", "act as", "from now on you are" followed by a model-ish role (AI, assistant, chatbot, DAN, unrestricted, jailbroken, "in developer mode" ...); "from now on you will ignore / answer without ..."; "developer mode" and "jailbreak" only next to AI, model, prompt or instructions words; "अब से तुम (एक) एआई ..."; "आतापासून तू (एक) एआय ...". Not: "you are now ready", "pretend to be a shopkeeper", "developer mode in Android", "अब से तुम रोज़ पढ़ाई करोगे" |
| `prompt_extraction` | "reveal / show / print / repeat your (system) prompt or instructions", "what are your instructions"; "अपने निर्देश बताओ"; "तुझ्या सूचना सांग" |
| `model_directive` | text addressed to a model ("AI: you must ...", "ChatGPT, always ...", "Chatbot: if a student asks ..."), or "if a student asks ..., the chatbot must say ..."; in Hindi and Marathi addressed to a model ("एआई: तुम्हें ...", "चैटबॉट, अगर कोई छात्र पूछे ..."). Not: a plain "If a student asks why ..., tell them ..." |
| `tool_call` | `<tool_call>`, `<function_call>`, `"function_call":`, `{"name": ..., "arguments": ...}`, `<\|tool\|>` |
| `link_directive` | only with a sign of exfiltration: send, upload, forward or "submit ... to" a URL ("भेजो", "पाठवा"); visit, open or click a URL whose query carries data (a parameter such as `data=`, `secret=`, `conversation=`, or a placeholder value such as `={...}`; also "खोलो", "उघडा"); a Markdown image or an HTML `<img src=...>` (they load by themselves). Not: "Visit https://ncert.nic.in", a YouTube `watch?v=` link |
| `encoded_blob` | 200 or more base64 characters in a row with at least one digit, `+` or `/` and at least one letter (not the digits of pi, not a DNA sequence) |

Matching runs on a normalised copy: the invisible characters ingest cleaning drops (format characters, variation selectors, fillers, controls) removed, Unicode tag characters turned into the ASCII they hide, NFKD and case folded, Cyrillic and Greek look-alikes of the trigger letters turned into Latin, combining marks after a Latin letter removed (never a Devanagari sign), NFKC, and every whitespace run collapsed to one space (or one line break). Words may be separated by spaces or `_ - . , / : ;`. Full-width letters, accents, `İ`, look-alikes, zero-width characters and runs of spaces make no difference. The stored text is never changed.

**Measured (5d-2b, `tests/fixtures/scan_corpus.py`):** 0 of 54 ordinary syllabus sentences (English, Hindi, Marathi, and computer-science lessons about prompts and chatbots) are flagged (the 5d-2 rules flagged 21); 20 of 20 reported cheap evasions are caught (the 5d-2 rules caught 0). Both tests print their numbers. The corpus was written for the test: it is not a false-positive rate of real files (B99).

**Limits.** This is a tripwire, not a guarantee:

- Paraphrases, other languages, text in images, a phrase split across two chunks and anything not foreseen pass **unflagged**. `flagged: false` never means safe.
- Text that is really **about** injection is still flagged: a lesson that quotes a jailbreak prompt, "tell the chatbot: ignore all previous instructions", or a note to "send the answers to https://...". Ordinary text about prompts and chatbots, plain links and plain "if a student asks" notes are not (5d-2b). Evasions still open: backlog B101.
- Not detectable per chunk: zero-width and bidi characters (removed at ingest), PDF hidden text (backlog B56), PPTX hidden slides (backlog B53).

| Code | HTTP | Where |
|---|---|---|
| `invalid_query`, `query_too_long` | 422 | test search |
| `test_search_rate_limit_exceeded` | 429 + `Retry-After` | test search; the body also has `retry_after_seconds` |
| `search_unavailable`, `search_timeout`, `search_busy` | 503 | test search |
| `service_unavailable` | 503 | test search: Redis is down outside development |

### 11.4 Validation errors on every route (5d-2b)

FastAPI's `422` keeps its shape, `{"detail": [errors]}`, but an error now carries only `type`, `loc`, `msg` and, when it holds the schema's own limits (`ge`, `le`, `max_length`, `pattern` ...), `ctx`. The echoed `input` is gone: for a missing field it was the whole request body, so a login without `tenant_slug` repeated the password (review of 5d-2, finding 8; tested on `/auth/login` with six bad bodies). Nothing in `frontend/src` read `input`.


## 12. Retrieval evaluation harness (5d-3)

`python -m app.scripts.eval_retrieval` measures **retrieval only** (no LLM) on a golden set of questions against the real published documents of one class and subject, with the real embedding model. It exists so the owner can choose `RETRIEVAL_MIN_SIMILARITY` from data. Plan: `docs/phase5d-plan.md` section 10. Code: `app/services/retrieval_eval.py` (pure metrics, no database) and `app/scripts/eval_retrieval.py` (the command).

### 12.1 What it does and never does

- Reads the golden CSV, finds the tenant by slug in a **READ ONLY** platform transaction, then runs everything in **one READ ONLY tenant transaction** as `app_tenant` (RLS on). Postgres refuses any write there (tested).
- The scope is `RetrievalScope.for_operator`: one offered class and subject pair, **published versions only**, the purpose `read` (what a student would see). `for_operator` is used only from `app/scripts` (a test scans `app/`).
- Every question goes through `retrieval.search`, the same service the API uses, with the settings of the environment it runs in (`RETRIEVAL_*`).
- It prints and reports **ids, titles, file names, pages and headings only. Never chunk text.** (`ChunkRef` has no content field; a test checks the output and the report.) Titles and headings are untrusted text from uploaded files, so the console copy is cleaned of control characters.
- It never sets a threshold. `RETRIEVAL_MIN_SIMILARITY` is read and shown; only `sufficient` follows it. The sweep (12.5) does not depend on it.

### 12.2 How to run it

Prerequisites: Docker is up (Postgres 5433, Redis); the documents are **published** in that class and subject; `tests/fixtures/golden_set.csv` exists (12.3); the real model is fetched and `EMBEDDING_MODEL_DIR` points at it (`python -m app.scripts.fetch_model e5-small --out <dir>`). The command uses `DATABASE_URL` and `PLATFORM_DATABASE_URL` from the environment (`backend/.env`), like the API, so **it runs against whatever database that file names**: check that it is the dev database you mean. The first run verifies and loads the model (about 0.5 GB; seconds to minutes).

PowerShell, from the `backend` folder, with the owner's values (tenant `acme`, class `469bc5e7-...`, subject `abc756c6-...`). The report goes outside the repository because it holds your questions and document titles:

```powershell
cd D:\Devnectar\rag-application\backend
.\.venv\Scripts\Activate.ps1
python -m app.scripts.eval_retrieval `
  --tenant acme `
  --class-id 469bc5e7-4382-49cb-8ad6-70d16e1639b3 `
  --subject-id abc756c6-0234-4db6-a09f-f596bfa4afca `
  --golden tests/fixtures/golden_set.csv `
  --k 10 `
  --report "$env:TEMP\retrieval-eval-acme.json"
```

| Argument | Meaning |
|---|---|
| `--tenant` | Tenant slug (required) |
| `--class-id`, `--subject-id` | The pair to evaluate (required; the tenant must offer it) |
| `--golden` | The CSV (default `tests/fixtures/golden_set.csv`, relative to where you run it) |
| `--k` | Hits per question, 1 to 50 (default 10). `hit@k` and MRR use it as the cutoff |
| `--report` | Where to write the JSON report (required; parent folders are created) |
| `--compare` | (5d-4) Run the golden set under many retrieval configurations and print ONE comparison table (12.8). Without it the command behaves exactly as before |
| `--grid` | (5d-4) With `--compare` only: a smaller grid such as `--grid vw=1,2 df=off,0.3 mt=1` (12.8) |

| Result | Exit code | Message |
|---|---|---|
| Done | 0 | the summary, then `report written: <path>` |
| Golden file, model directory or documents missing | **0** | `skipped: <reason>` (so a machine without the files, such as CI, is not an error) |
| Malformed golden file | 2 | `error: golden set row N: ...` (N is the spreadsheet row; the header is row 1) |
| Unknown tenant; a pair the tenant does not offer | 2 | `error: ...` |
| A question that cannot be searched (no searchable word, over 500 characters) | 2 | `error: golden set row N: ... (invalid_query / query_too_long)`, found before the model loads |
| A search timeout, or a model that fails verification | 2 | `error: ...` |
| `--grid` without `--compare`, or a grid that cannot be used | 2 | `error: --grid only works together with --compare` / `error: bad --grid: ...` (before anything is loaded) |

### 12.3 The golden CSV

UTF-8, with or without a BOM (Excel's "CSV UTF-8"). Columns are found by their header name, so their order does not matter; extra columns are ignored; blank lines are skipped. A question with a comma needs quotes (an unquoted comma gives "has 7 columns but the header has 6").

| Column | Rule |
|---|---|
| `#` | Your number for the question (shown in the summary; blank = the row number minus one) |
| `Language` | Free text such as `English`, `Marathi`, `Hindi`. Everything is split by this value |
| `Question` | The question as a student would type it (at most 500 characters) |
| `Document` | The document **title or original file name** (case, spaces and Unicode form do not matter; the file name may be written without its extension). Exact, never partial. `(none)` marks a question that should **not** be answered |
| `Expected location` | `slide N`, `slide N-M`, `page N`, `pages N-M` (hit when the chunk's page range overlaps; a hyphen, en dash or em dash all work), or `heading: <text>` (hit when that text, casefolded and NFC-normalised, is inside the chunk's heading path). Blank for a should-not-answer row |
| `Should answer` | `Yes` or `No` (also `y`, `n`, `true`, `false`, `1`, `0`). Blank follows the Document column. `(none)` together with `Yes` is an error |

A retrieved chunk is **the expected one** when its document matches the Document cell **and** its place matches the location. PPTX slides are stored as pages, so `slide 5` and `page 5` are the same test.

### 12.4 What each number means

| Number | Meaning | Read it as |
|---|---|---|
| **hit@1 / 3 / 5 / k** | Of the should-answer questions, the share whose expected chunk is among the first 1, 3, 5, k fused results | Retrieval quality. hit@5 is about what a chat answer would see |
| **MRR@k** | Average of 1 / (position of the expected chunk), 0 when it is not in the first k | 1.00 = always first |
| **answer rate** | Of the should-answer questions, the share whose best vector similarity is at or above the threshold (`sufficient = true`). It does not check that the chunk is the right one | Too low: the threshold refuses real questions |
| **refusal accuracy** | Of the should-not-answer questions, the share that stay below the threshold (refused) | Too low: the threshold lets unrelated questions through |
| **keyword only / vector only hit@k** | The same hit@k when only one side of the hybrid search is used | Input for the trigram decision (backlog B92): a Marathi or Hindi keyword-only rate far below the vector-only rate means inflected words are missed by the keyword side |

Everything is shown overall (`ALL`) and per `Language`. A rate is `n/a` when the group has no row of that kind (so "no refusal rows" never shows as 0%).

Each question also gets an **outcome**, and the summary lists every failure with its cause:

| Outcome | Plain English |
|---|---|
| `ok` | The expected chunk was retrieved and the answer would be given |
| `below_threshold` | **Found but below the threshold:** the expected chunk was retrieved, but the best similarity is under the threshold, so the question would be refused |
| `wrong_chunk` | Chunks were retrieved but the expected place is not among them |
| `no_candidate` | Nothing was retrieved at all |
| `document_not_in_scope` | The Document cell matches no published document of this class and subject (a typo, or the file is not published; backlog B80 explains why `en-01.pdf` cannot be) |
| `refused_correctly` / `answered_anyway` | For should-not-answer rows: stayed below the threshold, or was answered |

The JSON report has, per question, the top five hits (rank, chunk id, document title, pages, heading, vector similarity, keyword matched terms, fused score, whether it is the expected chunk), the hit position, the position in the keyword-only and vector-only rankings, the best similarity, the keyword coverage and the outcome. It also has the corpus: chunks per document, the flagged chunk count (injection scanner) and, for backlog B97, the keyword cap (`RETRIEVAL_KEYWORD_MAX_CANDIDATES`) next to the number of chunks in scope: the cap can only drop a better match when the scope has **more** chunks than the cap.

### 12.5 The threshold sweep and how to choose

The command runs **one search per question**, stores each question's best vector similarity (and keyword coverage), and computes the table for the thresholds 0.70 to 0.95 in steps of 0.01 with plain arithmetic. For each threshold: the answer rate (should-answer rows at or above it) and the refusal accuracy (should-not-answer rows below it). Two rows are marked:

- **best balanced threshold:** the highest mean of the two rates; if several thresholds tie, the highest of them (it refuses more);
- **highest threshold that keeps the answer rate at or above 90%:** the strictest value that still answers nine in ten real questions.

These are starting points, not decisions. How to choose:

1. Decide what is worse for your school: a wrong answer from unrelated material (a threshold too low) or a refusal of a real question (too high). The balanced row treats them as equal.
2. Look at the **plateau** around the marked rows, not one number: choose a value in the middle of a stable stretch, so a few questions do not flip it.
3. Look at the failure list: `below_threshold` rows with similarities just under your pick show what a lower value would recover; `answered_anyway` rows show what a higher one would stop.
4. e5 similarities cluster high (even unrelated text often scores 0.7 or more), so expect useful values in the upper half of the range. If no value gives both a high answer rate and a high refusal accuracy, the problem is retrieval quality (hit@k), not the threshold.
5. A small golden set (fewer than about 20 rows of each kind) gives a coarse table. Add questions before trusting a plateau.
6. The second sweep in the JSON report (`keyword_coverage_sweep`) is the same table for the keyword-coverage clause alone. `RETRIEVAL_MIN_KEYWORD_COVERAGE` stays `0` (off) unless that table shows a clear gain.

### 12.6 Turning the chosen value into the setting (by hand; the harness never does)

1. **Local run:** set `RETRIEVAL_MIN_SIMILARITY=<value>` in `backend/.env` (the environment of the API process) and restart the API. `.env` is never committed.
2. **The default:** change `RETRIEVAL_MIN_SIMILARITY` in `backend/app/core/config.py` (default `0.85`) and the same line, with its UNCALIBRATED comment, in `backend/.env.example`; say in that comment which model, which date and how many golden questions the value comes from.
3. **`threshold_calibrated=true`:** today this flag is **not a setting**. `RetrievalConfig.threshold_calibrated` (`app/services/retrieval.py`) is a dataclass default of `False` that `RetrievalConfig.from_settings` does not set, and the test-search response copies it. To report `true`, add a setting (for example `RETRIEVAL_THRESHOLD_CALIBRATED`, default `false`, with `.env.example` and a bounds test) and pass it in `from_settings`, or flip the dataclass default once the value is the default. This is a small follow-up step and was **not done in 5d-3** (backlog B105).
4. **Tests that pin the uncalibrated default** change with it: `tests/test_retrieval_pure.py` (`cfg.min_similarity == settings.RETRIEVAL_MIN_SIMILARITY == 0.85` and `threshold_calibrated is False`), `tests/test_retrieval_quality.py` (`result.threshold == 0.85`) and `tests/test_test_search_contract.py` (`threshold_calibrated is False`).
5. **The value belongs to the model** (`EMBEDDING_MODEL_ID`, response field `model`). A different model, or a different `query:` / `passage:` prefix, needs a new run and a new value. Record the model key, the date and the golden-set size next to the number.
6. Run the harness again with the new setting: the answer rate and refusal accuracy at the current setting should match its row in the table, and the questions that fail should be the ones you accepted.

### 12.7 Tests

`tests/test_retrieval_eval_metrics.py` (fast: loader, matcher, metrics, sweep and report on a synthetic golden set), `tests/test_eval_retrieval_script.py` (fast: skips, malformed CSV, arguments, the operator-scope guard) and `tests/test_eval_retrieval_db.py` (db: the whole command on synthetic documents with a scripted embedder, READ ONLY refused writes, skips and errors). 5d-4 adds `tests/test_retrieval_fusion_pure.py` and `tests/test_retrieval_compare_pure.py` (fast), `tests/test_retrieval_pruning.py` and `tests/test_eval_retrieval_compare_db.py` (db). The real golden set and the real model are never used by a test.

### 12.8 Comparing retrieval configurations: `--compare` (5d-4)

**Why it exists.** The first real run (14 questions, 100 chunks, e5-small) showed the hybrid search doing **worse** than the vector side alone: Marathi hit@1 40% hybrid against 100% vector only, overall hit@1 67% against 92%, and keyword-only Marathi hit@1 20%. The hypothesis (to be tested with this table, not assumed): the keyword query is an OR of *every* word of the question, so very common words (ची, आहेत, कोणती, काय, what, is, the) match most chunks; `ts_rank_cd` has no inverse document frequency, so those chunks rank as if they were good matches; and reciprocal rank fusion gives the noisy keyword ranks the same weight as the vector ranks. Backlog B106.

**The four settings (all default to today's behaviour; nothing changes until you choose values).**

| Setting | Default | What it does |
|---|---|---|
| `RETRIEVAL_KEYWORD_MAX_DF` | `1.0` (off) | A word of the question that appears in **more than** this share of the candidate chunks of the scope is left out of the keyword query. Frequencies are counted over the candidate chunks of that class and subject (published, not excluded), in the same statement that already counts them: **no extra round trip**. If no word is left, the keyword side is **not run at all** and the answer comes from the vector side. No stopword list: a word is "common" because the material says so, in any language; a rare code or name (an error code, a person) has a low frequency and stays. Caution on a tiny class: with 10 chunks, a word in 6 of them is 0.6 |
| `RETRIEVAL_RRF_VECTOR_WEIGHT` | `1.0` | Weight of the vector side in the fusion: `score = sum(weight_side / (RETRIEVAL_RRF_K + rank))`. Must be above 0 (the sufficiency decision reads the vector side) |
| `RETRIEVAL_RRF_KEYWORD_WEIGHT` | `1.0` | Weight of the keyword side. `0` = the vector side alone (a chunk that only the keyword side lists is left out of the fusion; the keyword ranking is still computed and reported) |
| `RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS` | `1` | A chunk enters the keyword ranking only if it contains at least this many of the (remaining) question words. If fewer words remain than this, the keyword side does not run |

**Defaults are neutral, and tested as such:** with the defaults the hit ids and the fused scores are the same numbers as before 5d-4 (the regression test compares them bit for bit with the unweighted formula, and was also run against the unchanged code), and the default SQL statements contain none of the new machinery. `RETRIEVAL_MIN_SIMILARITY` and `threshold_calibrated` are untouched.

**What `--compare` does.** It runs the golden set once per configuration through the **same** `retrieval.search` (the configuration is overridden per run; there is no copy of the SQL), embeds every question **once**, stays **READ ONLY** and changes no setting. Rows of the table:

1. `current settings`: the configuration exactly as the environment has it (the reference of every comparison);
2. `vector only`: the keyword weight set to 0, the baseline;
3. the grid. Default: vector weight `vw` in {1, 2, 3} × keyword max document frequency `df` in {off, 0.5, 0.3, 0.2, 0.1} × keyword min matched terms `mt` in {1, 2} = 30 rows, the keyword weight `kw` fixed at 1.

A smaller grid: `--grid vw=1,2 df=off,0.3 mt=1` (names `vw`, `df` (`off` or 0 to 1), `mt`; a name that is left out stays neutral, so `--grid df=0.3` is one row; at most 200 points). Run it exactly like 12.2 with two more arguments:

```powershell
python -m app.scripts.eval_retrieval `
  --tenant acme `
  --class-id 469bc5e7-4382-49cb-8ad6-70d16e1639b3 `
  --subject-id abc756c6-0234-4db6-a09f-f596bfa4afca `
  --golden tests/fixtures/golden_set.csv --k 10 `
  --report "$env:TEMP\retrieval-compare-acme.json" `
  --compare
```

**Reading the table.** One line per configuration: the tag (`current settings` / `vector only`), the settings (`vw` `kw` `df` `mt`), then for `ALL` and for **each language** the columns `hit@1 hit@3 hit@5 MRR` (should-answer questions only), then `answered` (should-answer questions at or above the current threshold) and `refused` (should-not-answer questions below it), then **`hit@1 vs current`**: `+W/-L` means W questions whose right chunk became first and L that stopped being first, compared with the `current settings` row. Read the wins and losses, not only the percentages: with 14 questions one question is 7 points. Answer and refusal numbers follow the vector similarity, which no configuration here changes much, so expect them to stay close to each other.

**The overfitting guard.** Above the table the command prints the number of should-answer and should-not-answer rows, and a `WARNING ... too few to trust small differences` when there are fewer than **20** should-answer or fewer than **8** should-not-answer rows. A grid of 30 configurations on a handful of questions will always show some configuration that looks better by luck. Prefer a setting that is better on **every** language and does not lose questions (`-0`), sits in the middle of a region of good rows rather than at one lucky cell, and still beats `vector only`; add questions to the golden set before trusting a small gain.

**After choosing:** put the values in `backend/.env` (the environment of the API process) and restart, then run the harness without `--compare` once: its numbers must equal the `current settings` row of the comparison. The normal report now also states the four values it ran with. If the best rows are still no better than `vector only`, the answer is to use `RETRIEVAL_RRF_KEYWORD_WEIGHT=0` (or a small weight) for now and to revisit the trigram question (backlog B92) with the comparison numbers.

The JSON report of a comparison holds numbers only (one entry per configuration with `overall`, `by_language`, `answered`, `refused`, `hit1_wins`, `hit1_losses`, plus `sample` with the row counts and warnings); it holds no question text and no chunk text.
