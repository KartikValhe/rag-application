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
| `pdf_active_content` | 422 | document upload **and timetable import** | PDF contains `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia` or `/XFA` (also written with `#xx` escapes). `/OpenAction` and `/AA` are **allowed**. |
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

Rejected with `pdf_active_content`: `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia`, `/XFA`, including `#`-escaped spellings such as `/J#61vaScript`. **Allowed:** `/OpenAction` and `/AA`. The object and page caps stay. Every regex of the scan uses bounded repetition only (for example `[0-9]{1,10}\s{1,10}[0-9]{1,5}\s{1,10}obj\b`), so the scan is linear in the file size; before the 5a-3 review 2 MB of digits behind a `%PDF-` header kept it busy for hours. An object or page written with unusual amounts of whitespace is simply not counted. Routes call `validate_upload_async`, which runs the validation in a worker thread for both profiles; a thread cannot be killed, so the linear algorithms are the protection and the thread only keeps the event loop responsive. The scan reads **uncompressed** bytes only: a name or a page object inside a compressed object stream is invisible to it, so it cannot be relied on to find active content or to count pages. It is a first filter. The sandboxed document parser arrives in 5b (its memory limit applies on Linux only) and must re-check the page count after parsing (backlog B33). The scan works in 1 MB slices, so its memory does not grow with the file.

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
| `POST .../versions/{version_id}/retry` | `document:upload` + manage on all classes | | `202` `DocumentVersionResponse` | 403 manage; 404; 409 `version_not_retryable`, `document_processing_in_progress`, `duplicate_version` |
| `DELETE .../versions/{version_id}` | `document:upload` + manage on all classes | | `204` | 403 manage; 404; 409 `version_not_deletable` |
| `GET .../versions/{version_id}/download` | `document:view` | | `200` file stream | 404 (also when the file is missing) |
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
- `DocumentUpdateRequest` (PATCH body): `{title?: string (1-200), class_ids?: uuid[] (1-50)}`. Any other key is a 422.
- `TenantDocumentLimitsResponse`: `{tenant_id, overrides: {key: integer}, effective: DocumentLimits, ceilings: DocumentLimits}`.

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
| `service_unavailable` | 503 | upload: the private store could not be written, or Redis is down outside development |
| `unknown_limit`, `invalid_limit_value`, `limit_above_ceiling` | 422 | platform limits endpoint |

The frontend error mapper for these codes and for the request-guard codes of section 2 (400 `duplicate_header` / `invalid_content_length`, 411, 415, 429, 503) is backlog item B36.
