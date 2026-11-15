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
| `too_many_slides` | 422 | document upload (5b) | A PPTX has more slides than the tenant's slide limit. |
| `file_too_large` | 413 | document upload | The body or file exceeds a limit: the middleware ceiling (50 MB + 64 KB body), the tenant's own file limit (default 10 MB), or the platform ceiling of 50 MB. |
| `too_many_parts` | 413 | document upload | More than 6 multipart parts. |
| `pdf_active_content` | 422 | document upload **and timetable import** | PDF contains `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia` or `/XFA` (also written with `#xx` escapes). `/OpenAction` and `/AA` are **allowed**. |
| `pdf_too_complex` | 422 | document upload and timetable import | PDF has more than 20,000 objects (timetable: also more than 100 pages). |

All upload errors keep the Phase 4 shape `{"detail": {"code", "message"}}` with a **fixed message per code** (no file names, member names, paths or library text). The other codes a document upload can return (`empty_file`, `disallowed_file_type`, `file_type_mismatch`, `invalid_openxml_format`, `macro_enabled_file`, `embedded_content`, `external_relationship`, `invalid_xml`, `xml_entity_expansion_forbidden`, `corrupt_archive`, `encrypted_file`, `zip_bomb_detected`, `zip_member_too_large`) are the Phase 4 codes with the same meaning.

## 3. Upload rules for documents (step 5a-3)

Implemented in `app/core/upload_security.py` (`UploadProfile.DOCUMENT`, `validate_upload`), `app/core/body_limit.py` and `app/services/document_store.py`. The routes that use them come in 5b.

### 3.1 Per-route body limits (middleware, before auth)

| Path prefix | Body limit | Multipart parts |
|---|---|---|
| `/api/v1/documents` (the path and everything below it, any request type) | 50 MB + 64 KB | 6 |
| everything else (multipart requests and the timetable import and template routes) | 2 MB + 64 KB | 5 |

Over the limit the answer is `413` with `file_too_large` (or `too_many_parts`), before the body is read; a body without `Content-Length` is counted while it streams. The middleware cannot know the tenant, so a tenant's own limit (default 10 MB) is enforced by the route while it streams the file. A file of exactly 50 MB fits the documents limit; the platform ceiling `DOC_UPLOAD_CEILING_BYTES` (50 MB) is also a hard cap inside `stream_upload_with_limit`.

### 3.2 Accepted formats and caps (`validate_upload`, `UploadProfile.DOCUMENT`)

- **Formats:** PDF, DOCX, PPTX, TXT, MD. The type comes from the content; the extension must agree (`file_type_mismatch` otherwise). Text must be UTF-8 (BOM allowed, else `invalid_encoding`); `.md` or `.txt` is decided by the extension once the content is known to be text; any other extension is `file_type_mismatch`. CSV and XLSX give `unsupported_format`.
- **Per-file size:** a parameter (the tenant's `max_file_bytes`), never above 50 MB.
- **Zip caps (OpenXML):** 60 MB uncompressed in total, 25 MB per member, compression ratio 100, at most 1,000 members (timetable imports keep 15 MB, 5 MB, 50 and 200).
- **OpenXML rules** (same as Phase 4): no macros, no `embeddings/`, no nested zip or OLE inside the package, no external relationships except plain hyperlinks (never fetched), XXE and entity expansion blocked, every XML check fails closed. For PPTX the rules also cover the `ppt/` parts: every XML part under `ppt/` must parse (`invalid_xml` / `xml_entity_expansion_forbidden`), `ppt/activeX/` is `embedded_content`.
- **Page and slide caps** are parameters: PDF pages (default 200, `too_many_pages`), PPTX slides (default 100, `too_many_slides`).

### 3.3 The PDF rule (documents and timetable imports)

Rejected with `pdf_active_content`: `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia`, `/XFA`, including `#`-escaped spellings such as `/J#61vaScript`. **Allowed:** `/OpenAction` and `/AA`. The object and page caps stay. The scan reads **uncompressed** bytes only: a name inside a compressed object stream is invisible to it, so the sandboxed parser (which never renders or executes anything) is the real protection.

### 3.4 Private document store

Originals are kept in a **private** store (`get_document_store()`), separate from the public branding store: its own root (`DOCUMENT_STORE_DIR`), no URL method, never served by a route. Keys are `t/{tenant_id}/d/{32 random hex}`; every call names the caller's tenant and any key outside that tenant's prefix (or in another shape) raises `InvalidStorageKey`. Downloads are streamed through an authenticated route (5b). Supabase Storage replaces the local backend later behind the same interface.
