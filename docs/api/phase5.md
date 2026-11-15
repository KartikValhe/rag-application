# Phase 5 API Reference: Documents

Started during the 5a-2 security hardening. The document endpoints themselves (upload, replace, list, retry, delete, purge, test search) are specified in `docs/phase5-design.md` (A3, A4) and get their full contract here in step 5e. This page lists the contract changes that already exist and the upload rules built in step 5a-3 (section 3).

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

Originals are kept in a **private** store (`get_document_store()`), separate from the public branding store: its own root (`DOCUMENT_STORE_DIR`), no URL method, never served by a route. Keys are `t/{tenant_id}/d/{32 random hex}`; every call names the caller's tenant and any key outside that tenant's prefix (or in another shape) raises `InvalidStorageKey`. Writes are atomic (temp file in the same directory with `O_EXCL` and mode 0600, `fsync`, then `os.replace`; a failed or interrupted write leaves no file); directories are 0700 (best effort on Windows, which ignores POSIX modes). `stream()` opens the file only when iteration starts and closes it in `finally`. The public branding store's `get_url` refuses a document key. Downloads are streamed through an authenticated route (5b). Supabase Storage replaces the local backend later behind the same interface.
