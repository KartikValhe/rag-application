# Phase 5f plan: frontend for "Course materials"

Status: PLAN ONLY (5f-0, 2026-11-20). Nothing in `frontend/` or `backend/` was changed to write it. Not committed.
Read with: `docs/api/phase5.md` (sections 1-2, 4-9, 11), `docs/phase5d-plan.md`, `docs/backlog.md` (B20, B36, B77, B82, B104, B94, B98, B100), `CLAUDE.md`.
Backend and frontend are built one after the other. This plan needs **no backend change**; two small optional backend additions are raised as questions (Q3, Q4).

## 0. Facts checked first

| # | Fact | Where it comes from |
|---|---|---|
| F1 | The frontend test runner is `node --test --experimental-strip-types` on an **explicit file list** in `package.json`. It runs `.ts` files only: no JSX, no DOM, no `@/` alias (tests import with relative paths and a `.ts` extension), and `import.meta.env` is undefined. So `src/api/client.ts` cannot be imported by a test. There is **no component-test tool** (no Vitest, jsdom or Testing Library). | `package.json`, `src/features/timetable/__tests__/*` |
| F2 | The shared error mapper `src/api/errorMessages.ts` is keyed by code only and holds **timetable-specific texts that are wrong for documents**: `file_too_large` says "2 MB"; `disallowed_file_type` and `unsupported_format` say "Use CSV, XLSX, DOCX or a text PDF" (XLSX and CSV are refused for documents); `scanned_pdf` says "Use a text PDF or a CSV"; `pdf_active_content` says "scripts or attachments" (stale since `pdf_embedded_file`, B82). | `errorMessages.ts` |
| F3 | Permissions come from the **JWT claims** (`permissions`, `roles`, `is_admin`) read in `AuthContext`; `usePermission().can()` treats admins as holding everything. The code says these checks are cosmetic. Route guards are role based (`AdminRoute`, `TeacherRoute`, `StudentRoute`) and show the 403 page. Sidebar items have an optional `permission`. | `usePermission.ts`, `guards.tsx`, `Sidebar.tsx` |
| F4 | System roles: the teacher holds `document:upload`, `document:delete`, `document:view`, `document:publish`; the student holds none; the tenant admin holds all (and `settings:manage`, needed for usage). | `backend/app/services/rbac.py` |
| F5 | `GET /me/classes` (permission `chat:use`) gives admins all classes with their subjects and teachers their assigned class and subject pairs. That is the data for the class/subject pickers of both roles. Students get their enrolled classes from the same route, but every document route answers them `403`. | `academic.py` 1170 |
| F6 | Nothing polls today (`refetchInterval` appears nowhere). The default query options are `retry: 1`, `refetchOnWindowFocus: false`. | `App.tsx` |
| F7 | `apiClient` has a **15 s timeout**. Uploads and downloads need their own (the timetable code uses 60 s `IMPORT_TIMEOUT_MS`; documents can be 50 MB). The response interceptor refreshes the token on a 401 and replays the request once. | `client.ts` |
| F8 | `FileUpload` is not accessible: the drop zone is a `div` with `onClick` (no keyboard, no role), the real input is `hidden` with a fixed `id`, the remove button has no label. | `FileUpload.tsx` |
| F9 | The style guard forbids inline `style=` and hex colours. A progress bar whose width comes from data therefore cannot use a style attribute: use the native `<progress>` element or a fixed set of width classes. | `scripts/check-styles.js` |
| F10 | `docs/api/phase5.md` section 5 omits `matches` from the `FlaggedChunk` model, but `backend/app/schemas/document.py` has it (`matches: list[FlagMatch]`; section 11.2 and B104 describe it). The code is right. | `schemas/document.py` 205 |
| F11 | **Teachers cannot read their tenant's limits.** `GET /admin/documents/usage` needs `settings:manage`, so the upload dialog cannot show the tenant's file-size limit (default 10 MB, ceiling 50 MB). The UI can only check type, "not empty" and the 50 MB ceiling, and must map the server's `413 file_too_large` into a plain message without a number. See Q3. | `phase5.md` 4.1 |
| F12 | A list item has `latest_version.error_code` but **no `retryable` flag**; `retryable` exists only on the status route. The rule (transient codes `processing_failed`, `worker_crash`) is a backend rule. See Q4. | `schemas/document.py` |
| F13 | CLAUDE.md says "Do NOT edit `frontend/` (another agent owns it)" and lists `npm run test:e2e`, but `package.json` names the script `e2e`. CI runs `npm test -- --run`. | `CLAUDE.md`, `package.json`, `ci.yml` |
| F14 | The class pages carry "Coming in Phase 5" placeholders: `TeacherClassDetailPage` (replace), `StudentClassDetailPage` (says instructor uploads will appear for students: contradicts "students see none of this", see Q8) and `DashboardPage`. | `grep` |

## (a) What exists today and what we reuse

| Need | Reuse | Change needed |
|---|---|---|
| HTTP, auth, tenant header, token refresh | `apiClient` | Per-request `timeout` for upload and download. Optional: refresh the token before a big upload (see section (e), upload) |
| Error to text | `parseApiError`, `getFriendlyError` (network, schema mismatch, `Retry-After`, per-status fallbacks) | **New** `features/documents/utils/documentErrors.ts` with its own table, checked first; the shared mapper stays for everything else. Add `pdf_embedded_file` and `subject_has_documents` to the shared table (B82, B20) |
| Response validation | The Zod + `validateResponse` + `SchemaMismatchError` pattern of `timetableSchemas.ts` | New `documentSchemas.ts` (lenient objects, see section (g)) |
| Server state | TanStack Query (`useQuery`, add `useInfiniteQuery` for the cursor list) | Polling options per query |
| Layout | `AppShell`, `PageHeader` (breadcrumbs, actions), `ForbiddenState`, `NotFoundState`, route guards | New routes (section (c)) |
| UI kit | `Button`, `Badge` (success, warning, danger, info, primary), `Card`, `Table` (stacked cards below `md`, "Actions" column goes to the card footer), `Modal` (focus trap, scroll lock, `aria-modal`, sticky footer, bottom sheet on mobile), `ConfirmDialog`, `Alert`, `EmptyState`, `Skeleton`, `Spinner`, `Select`, `Checkbox`, `Tabs`, `Toast` | `FileUpload` accessibility fix (F8) |
| Permission gating | `usePermission`, `Can`, sidebar `permission` field | A pure policy module on top (section (c)) |
| Class and subject pickers | `academicApi.getMyClasses()` (`/me/classes`) | none |
| File helpers | `sanitizeDisplayFilename`, `validateClientFile` pattern (`importWizard.ts`) | New document variant (extensions, 50 MB ceiling); do not reuse the timetable one (CSV, 2 MB) |
| Blob download with auth header | `timetableApi.downloadTemplate` + `unwrapBlobError` | Only for the optional download (section (h), 5f-11) |
| Tests | `node:test` files, `package.json` list, Playwright `world` fixture, `check-styles` | See section (g) |

## (b) Screens

New feature folder `frontend/src/features/documents/` (`api/`, `schemas/`, `utils/`, `hooks/`, `components/`, `pages/`, `__tests__/`). Words on screen say "Course materials", "version", "draft" (a `ready` version), "published". Internal words (`ready`, `stage`, `chunk`, `embedding`) never appear.

### b1. Materials list (`/teacher/materials`, `/admin/materials`)
- Header "Course materials"; action "Upload material" (only with `document:upload`).
- Filters: **class** and **subject** (selects from `/me/classes`; the subject select lists the subjects of the chosen class, "All subjects" allowed), **status** (All, Processing, Draft, Published, Failed). Filters live in the URL query (`?class=&subject=&status=`) so a link and the browser Back button work. They map to `GET /documents?class_id&subject_id&status&limit=20`.
- One row per document: title, subject, class chips (first 3 and "+n"), the **status chip** of the latest version, "Published: v3" if a published version exists and differs from the latest, size and type, updated date. Actions in the last column (Table "Actions" goes to the mobile card footer).
- Cursor paging: "Load more" (`useInfiniteQuery`, `next_cursor` opaque, never parsed).
- Status chips (text always visible, colour only helps; icon plus text):

| Version status | Chip text | Variant | Note |
|---|---|---|---|
| `queued` | Waiting | info | after 5 minutes: "Waiting for the processing service" hint (b2) |
| `processing` + `stage` | Reading file / Splitting text / Checking text / Indexing (stages `parsing`, `chunking`, `scanning`, `embedding`, `indexing`; `scanning` is allowed by the DB but the worker scans inside the index stage, so keep a mapping for it anyway) | info | unknown stage: "Processing" |
| `ready`, `flagged_chunk_count` 0 | Draft, ready to publish | primary | |
| `ready`, `flagged_chunk_count` > 0 | Draft, needs review (N passages) | warning | |
| `ready`, `flagged_chunk_count` null | Draft, not checked yet | warning | "not scanned" is not "clean" (backend rule) |
| `published` | Published | success | |
| `failed` | Failed | danger | plain reason from `error_code` (b3) |
| `archived` | Older version | default | detail page only |
| any other string | Unknown | default | a new backend status must not break the page |

- When a document has a published version **and** a newer draft/processing/failed latest version, show both: "Published v2" chip and the chip of v3.
- `can_manage === false`: show a small lock note "You teach this subject in only some of the linked classes, so you can only view it" (from `blocked_reason`, never the raw code).
- Empty states: no documents ("No materials yet" + Upload button if allowed), no result for the filters ("No materials match; clear filters"), no classes/subjects assigned (teacher: "You have no assigned classes yet; ask your administrator").

### b2. Document detail (`/teacher/materials/:documentId`, `/admin/materials/:documentId`)
Tabs: **Versions**, **Flags** (5f-6), **Test search** (5f-8, pre-scoped to this document's class and subject).
- Header: title, subject, classes, can_manage note, action menu (rename and relink are out of scope, Q9).
- Versions table: version number, file name (untrusted text), type, size, pages, status chip, uploaded date, who/when published, actions (Retry, Publish, Delete version, Review flags).
- A `duplicate_content` entry in `warnings` of an upload response is shown once after upload as an info note, not stored.
- Polling of the active version (section (e)).

### b3. Upload dialog (modal, 5f-3) and "New version" (same dialog, no title/subject/classes, 5f-5)
Fields: title (1-200 after trim, shown counter), subject, classes (checkbox list of the classes that offer this subject **and** are in the user's pairs, 1-50), file. Client checks: extension `.pdf .docx .pptx .txt .md`, not empty, at most 50 MB, with plain messages (XLSX/CSV: "Spreadsheets are not accepted here; export it as PDF or DOCX"). Progress bar (native `<progress>`), Cancel (aborts the request), submit disabled while sending. On 202: close, toast "Uploaded. We are reading the file now.", refetch list, show the new row as Waiting. Server errors map through the document mapper (section (d)); a field-level code (`invalid_title`, `too_many_classes`) shows beside its field, a file code beside the file zone, everything else in an `Alert` inside the dialog (the dialog stays open, the form keeps its values, the file stays selected).

### b4. Retry failed (5f-4)
Version row action "Try again" for a failed version whose status response says `retryable`. Not retryable failures show the plain reason and "Upload a corrected file as a new version". Confirmation is not needed (not destructive). 202 puts the version back to Waiting and polling resumes. Codes in (d).

### b5. Delete (5f-4 version, 5f-5 document)
`ConfirmDialog` naming the item: "Delete version 3 (notes.pdf)?" only for `ready` or `failed` versions; "Delete 'Chapter 4 notes'? Students will no longer see it and it cannot be restored from here." for a document. Both need the item's own permission and `can_manage`. On success: toast, refetch, and for a document go back to the list. 404 after the click means someone else deleted it: treat as success ("already removed") and refetch.

### b6. Flags viewer (5f-6)
Read-only list for one version (`GET .../flags`, `after_ordinal`, `limit` 50, "Load more"): heading "N passages were flagged for review" (`flagged_chunk_count`; null: "This file was never checked. An administrator can run a rescan; you can still publish after acknowledging"). Header notes when `title_flagged` or `filename_flagged` is true. Each item: page range ("Page 3" or "Pages 3-4"; for a PPTX these are slide numbers, label "Page/slide" unless the file kind is known from the version), heading path, one chip per code with a plain description (table below), the matched span(s) (`matches[].source` says "in the heading" or "in the text"; show `matches[].excerpt`; fall back to `excerpt` when `matches` is empty, and say "No longer matches the current rules" for a stored code without a match), and an "Excluded" marker. **All of it is untrusted text: render as React text with `whitespace-pre-wrap break-words`, never HTML or Markdown** (guard in section (g)).

| Code | Plain description |
|---|---|
| `instruction_override` | Tells an AI to ignore its instructions |
| `role_markup` | Looks like chat-system markup |
| `role_play` | Tells an AI to act as something else |
| `prompt_extraction` | Asks an AI to reveal its instructions |
| `model_directive` | Gives orders to an AI |
| `tool_call` | Looks like a tool or function call |
| `link_directive` | Links or images that could send data out |
| `encoded_blob` | A long encoded block of text |
| other | "Flagged for review" |

A line states the limits in plain words: "This check is a tripwire. Text that is not flagged is not guaranteed safe" (backend section 11.3).

### b7. Publish with flag review (5f-7)
- "Publish" (version `ready`, `document:publish`, `can_manage`). Click opens a dialog:
  - `flagged_chunk_count === 0`: simple confirm. Body is sent **without** `acknowledge_flags`.
  - `> 0` or `null`: the dialog first loads the flags (b6 inside the dialog), shows the count, the first page of items and "Review all N" link to the Flags tab, a warning if the title or file name is flagged, and a **required checkbox** "I have read the flagged passages and want to publish this version". The Publish button stays disabled until it is ticked. Only then is `{acknowledge_flags: true}` sent. The UI never sends `true` by default.
  - If a version showed 0 flags but the server answers `409 flags_acknowledgement_required` (race, or a rescan), reload the version, open the review variant of the dialog and use `reason` (`flagged` or `not_scanned`) for the wording.
- Text when a published version exists: "Version 2 stays visible to students until you publish this one; then it is replaced."
- Results: `409 version_not_ready` (reload, "This version is no longer ready"), `409 publish_conflict` (reload, "Someone else published at the same time; check the result"), `403 document_manage_requires_all_classes`, `404`.
- B98: nothing in the UI promises that a title edited after publish is rechecked.

### b8. Test search (5f-8)
Panel (inside the detail page and also as a tab on the list page, scoped by the same filters): question box (counter, soft limit 500 characters; the schema caps raw input at 4,000), class and subject narrowing (default: everything the user can see), checkbox "Include drafts" (default off, help: "Also search versions that are ready but not published"), result count select 1-10 (default 6). Submit is disabled only while a search is in flight and while the question is empty.
- Result header (always shown, even with 0 hits): a banner from `sufficient` + `reason` + `message`: reasons `similarity`, `keyword_coverage` ("Enough matching material was found"), `below_threshold` ("The best match is below the threshold, so the student assistant would answer 'not enough information'"), `no_candidates`, `empty_scope`, unknown reason: show `message` if present else a neutral line. Hits are shown even when not sufficient (backend contract), with the note that the assistant would not use them.
- Threshold line: "Threshold 0.84" plus a chip from `threshold_calibrated`: **false** = warning "Not calibrated for the current search model (`model`); similarity numbers may mislead", **true** = "Calibrated for this model". Also `candidate_count` ("searched N passages").
- Each hit: rank, document title (link to detail) with version number and a chip (**Draft** for `version_status` ready, Published), page range, heading path, **similarity** (`vector_similarity`, three decimals, with "above/below threshold" text), the chunk text (plain text, first ~6 lines, "Show more"), flag chips (`flagged` codes) and warnings for `title_flagged` / `filename_flagged`. `keyword_rank` and `keyword_matched_terms` are **null by default** (vector-only since 5d-5): show the keyword line only when not null; `fused_score` and `vector_rank` go behind a "Technical details" disclosure. A null similarity shows "-", never 0.
- Errors: `429 test_search_rate_limit_exceeded` (wait text from `retry_after_seconds`), `503 search_unavailable` / `search_timeout` / `search_busy` / `service_unavailable` (retry text; a first search after a restart can be slow: show "Loading the search model, this can take a few seconds" after 3 s of waiting), `422 invalid_query` / `query_too_long` (beside the box), `404` (the class/subject is not yours: "Choose one of your own classes").
- The query is never stored, logged or put in the URL.

### b9. Admin usage (5f-9, `/admin/materials/usage`, tab "Usage" on the admin materials page)
Needs `settings:manage`. Three meters (documents / `max_documents`, storage bytes / `max_storage_bytes`, pages / `max_pages`) as native `<progress>` with text "120 of 200 (60%)"; storage in KB/MB/GB; amber text at 80%, red at 100%, plus plain words, not colour only. A limits table (file size, PDF pages, slides, uploads per user and per tenant per hour). Soft-deleted documents do not count (stated). No purge control: there is no route that lists deleted documents (purge takes an id), so it cannot be built without a backend addition; recorded as deferred.

## (c) Who sees what, and how permissions are checked in the UI

### Routes and entry points
| Route | Guard | Notes |
|---|---|---|
| `/teacher/materials`, `/teacher/materials/:documentId` | `TeacherRoute` (teacher or admin) + `document:view` | sidebar "Course materials" in the Teacher Portal section |
| `/admin/materials`, `/admin/materials/:documentId`, `/admin/materials/usage` | `AdminRoute` + `document:view`; usage also `settings:manage` | sidebar item in "Administration" |
| anything else | unchanged | A student who types a materials URL gets the existing 403 page (as for admin URLs today) |

The teacher class page placeholder becomes a "Course materials for this class" link (`/teacher/materials?class=<id>`). Students: no sidebar item, no route, no import of the documents feature from any student page (tested).

### Action matrix (cosmetic gating; the server decides)
| Action | Needs permission | Plus, from the API response | Hidden when | Shown but disabled (with a reason) when |
|---|---|---|---|---|
| See list, detail, flags, status | `document:view` | none | no `document:view` | n/a |
| Upload new material | `document:upload` | none | no permission | n/a |
| New version | `document:upload` | `can_manage` | no permission | `can_manage` false ("you teach it in only some of the linked classes"), or latest version is queued/processing/ready ("Publish or delete the draft first") |
| Retry | `document:upload` | `can_manage`; status route `retryable` | no permission, or version is not failed | not retryable: no button, plain reason instead |
| Delete version | `document:upload` | `can_manage` | no permission, or status not `ready`/`failed` | `can_manage` false |
| Delete document | `document:delete` | `can_manage` | no permission | `can_manage` false |
| Publish | `document:publish` | `can_manage`; version `ready` | no permission, or version not `ready` | `can_manage` false |
| Test search | `document:view` | none | no permission | n/a |
| Usage | `settings:manage` | none | no permission | n/a |

Rule of thumb: **hide** what the user can never do (missing permission, wrong status); **show disabled with a one-line reason** what they could do for another item but not this one (`can_manage` false). `can_manage` and `blocked_reason` come from the API and are never recomputed.

### How it is implemented
- One pure module `utils/documentPolicy.ts`: `getDocumentActions({ can, document, version, statusInfo }) => Array<{ id, visible, enabled, reason }>`. Components render only what it returns; nothing in a component calls `can()` for a document action.
- The JWT permissions can be stale until the next refresh and the UI check is cosmetic. Any `403 permission_denied` or `403 document_manage_requires_all_classes` from an offered action: show the mapped message, refetch the item (its `can_manage` may have changed) and do not retry.
- Students: all `document:*` routes answer them 403; the UI never calls them. If one is ever reached, show the standard 403 page.
- A user with a **custom role** that has `document:view` but is neither teacher nor admin has no portal today (guards are role based) and the backend gives such a user an empty scope. Not supported in v1 (Q7).

## (d) One error mapper

`features/documents/utils/documentErrors.ts`:
- `getDocumentError(err, context) => { message, field?: "title"|"file"|"classes"|"query", retryAfterSeconds?, action?: "reload"|"login"|"none" }`. It reads `detail.code` through the existing `parseApiError`, looks the code up in its **own** table, and falls back to the shared mapper for network, schema mismatch and plain statuses. A fixed table of plain English sentences; **never** the server `message`, a file name, a path or a code. `context` (`"upload"`, `"publish"`, `"search"`, ...) only selects a wording where one code means different things (for example `service_unavailable`).
- `getVersionFailure(error_code) => { message, retryable?: boolean }` for the `error_code` stored on a failed version (the worker's codes are never an HTTP body).
- 429 messages append the wait ("Please try again in 45 seconds"), taken from the `Retry-After` header or `retry_after_seconds`. 422 from FastAPI has `detail` as a list and no code: generic "Some of the values are not valid".

### Every code the backend can return on these routes (HTTP / where / plain message)
Messages are drafts for the owner to review (Q6).

**Auth and generic (any route)**
| Code | HTTP | Message |
|---|---|---|
| `permission_denied` | 403 | You do not have permission to do this. |
| `document_manage_requires_all_classes` | 403 | You can view this material, but you teach this subject in only some of its classes, so you cannot change it. Ask an administrator. |
| `not_found` | 404 | We could not find this material. It may have been removed. |
| `missing_token`, `invalid_token` | 401 | Your session has ended. Please sign in again. (the client first tries a refresh) |
| `scope_resolution_failed` | 500 | We could not check your access. Please try again. |
| `internal_error` | 500 | Something went wrong on our side. Please try again. |
| FastAPI validation list (no code) | 422 | Some of the values are not valid. Please check them and try again. |

**Request guard (upload routes and every `/documents` route)**
| Code | HTTP | Message |
|---|---|---|
| `duplicate_header`, `invalid_content_length` | 400 | The upload was not valid. Please try again. |
| `length_required` | 411 | The upload could not be sent. Please try again with a different browser or network. |
| `file_too_large` | 413 | This file is larger than your organisation allows. Make it smaller or split it. |
| `too_many_parts` | 413 | The upload was not valid. Upload one file at a time. |
| `unsupported_media_type` | 415 | The upload was not valid. Please try again. |
| `too_many_concurrent_uploads` | 429 (+Retry-After 5) | Several uploads are running from your network. Please try again in a few seconds. |
| `service_unavailable` | 503 | Uploads, search or storage are temporarily unavailable. Please try again in a few minutes. |

**Upload and new version: file checks (422)**
`unsupported_format` (Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets and CSV are not accepted.), `disallowed_file_type` (same wording), `file_type_mismatch` (The file extension does not match the file content.), `invalid_encoding` (The text file is not UTF-8. Save it as UTF-8 and upload again.), `empty_file` (The file is empty.), `too_many_pages` (This PDF has more pages than your organisation allows.), `too_many_slides` (This presentation has more slides than your organisation allows.), `xml_too_complex`, `invalid_xml`, `xml_entity_expansion_forbidden`, `corrupt_archive`, `zip_bomb_detected`, `zip_member_too_large`, `invalid_openxml_format` (the file looks damaged or is not a valid Office file), `macro_enabled_file` (Files with macros are not allowed. Save a copy without macros.), `embedded_content` and `external_relationship` (Files with embedded or linked content are not allowed.), `encrypted_file` (Password-protected files are not supported. Remove the password and upload again.), `pdf_active_content` (PDFs with scripts, launch actions or rich media are not allowed.), `pdf_embedded_file` (This PDF has another file attached inside it. Export or print it again without attachments, or upload a DOCX version instead. This is the backend's own fixed text, B82), `pdf_too_complex` (This PDF is too large or complex to process.).

**Upload and new version: other**
| Code | HTTP | Message |
|---|---|---|
| `invalid_title` | 422 | The title must not be empty (and at most 200 characters). Field: title |
| `too_many_classes` | 422 | A material can be linked to at most 50 classes. Field: classes |
| `quota_documents_exceeded` | 422 | Your organisation has reached its document limit. Ask an administrator to remove old materials. |
| `quota_storage_exceeded` | 422 | Your organisation has reached its storage limit. |
| `quota_pages_exceeded` | 422 | Your organisation has reached its page limit. |
| `duplicate_version` | 409 | This exact file is already an active version of this material. (carries `version_id`: link "Open it") |
| `document_processing_in_progress` | 409 | A version of this material is still being processed or waiting to be published. Publish or delete it first. |
| `rate_limit_exceeded` | 429 | You have uploaded too many files this hour. + wait text |
| `tenant_rate_limit_exceeded` | 429 | Your organisation has reached its hourly upload limit. + wait text |
| 404 `not_found` | 404 | the class or subject is not yours (generic) |

**List and status**
`invalid_cursor` (422: The list could not continue. Reload the page.), `invalid_status` (422: Unknown status filter; the UI only offers valid values, so treat as reload).

**Retry**
| Code | HTTP | Message |
|---|---|---|
| `version_not_retryable` | 409 | This file failed for a reason a retry cannot fix. Upload a corrected file as a new version. |
| `document_processing_in_progress` | 409 | Another version of this material is being processed. Try again when it is done. |
| `duplicate_version` | 409 | The same file is already an active version. |
| `retry_limit_reached` | 409 | This file has been retried too many times. Upload it again as a new version. |
| `retry_rate_limit_exceeded`, `retry_version_rate_limit_exceeded` | 429 | You have retried too often. + wait text |
| `service_unavailable` | 503 | as above |

**Publish**
| Code | HTTP | Message |
|---|---|---|
| `version_not_ready` | 409 | This version is not ready to publish (it may still be processing, or it was already published or changed). Reload the page. |
| `flags_acknowledgement_required` | 409 (+ `reason`: `flagged` or `not_scanned`) | Not an error message: opens the review (b7). `flagged`: "Some passages were flagged and must be reviewed." `not_scanned`: "This file has not been checked yet and must be reviewed." |
| `publish_conflict` | 409 | Another version was published at the same time. Reload and check the result. |
| 403 / 404 | | as above |

**Delete version / delete document**
`version_not_deletable` (409: Only drafts and failed versions can be deleted.), 403 `document_manage_requires_all_classes`, 404.

**Subject delete (existing admin screen, B20)**
`subject_has_documents` (409: This subject still has course materials, including recently deleted ones. Remove them before deleting the subject.)

**Test search**
| Code | HTTP | Message |
|---|---|---|
| `invalid_query` | 422 | Type a question with at least one word. Field: query |
| `query_too_long` | 422 | The question is too long. Shorten it (up to 500 characters). Field: query |
| `test_search_rate_limit_exceeded` | 429 | You have searched too often. + wait text |
| `search_unavailable` | 503 | Search is temporarily unavailable. Please try again in a few minutes. |
| `search_timeout` | 503 | The search took too long. Please try again. |
| `search_busy` | 503 | Search is busy right now. Please try again in a moment. |
| `service_unavailable` | 503 | Search is temporarily unavailable. |
| 404 | | The class or subject is not one of yours. |

**Usage**: 403 `permission_denied` only. **Platform limits and purge** are not part of this UI.

**Version failure codes (`error_code` on a failed version; `getVersionFailure`)**
| Code | Retry? | Message |
|---|---|---|
| `processing_failed` | yes | We could not process this file after several tries. Try again, or upload it again. |
| `worker_crash` | yes | Processing stopped unexpectedly. Try again. |
| `job_time_limit_exceeded` | no | Processing took too long. Upload a smaller or simpler file. |
| `scanned_pdf` | no | The PDF looks scanned: it has almost no selectable text. Upload a text PDF or a DOCX. |
| `unreadable_text` | no | The text uses a font that cannot be read (garbled characters). Export the file again with a standard font. |
| `no_text_found` | no | No text was found in the document. |
| `text_too_large` | no | The document contains more text than can be processed. Split it into smaller files. |
| `too_many_pages`, `too_many_slides` | no | The file has more pages or slides than your organisation allows. |
| `corrupt_pdf` | no | The PDF could not be read. Export it again. |
| `password_protected_pdf` | no | The PDF is password protected. Remove the password and upload it again. |
| `pdf_too_complex` | no | The PDF is too large or complex to process. |
| `pdf_embedded_file`, `pdf_active_content` | no | same texts as at upload |
| `parse_timeout` | no | Processing the document took too long. |
| `parse_failed`, `parse_output_invalid`, `parse_output_too_large`, `xml_too_complex` | no | The document could not be processed. Try exporting it again or upload a different format. |
| `quota_pages_exceeded` | no | Your organisation has reached its page limit. |
| every upload validation code of the table above | no | same texts (the parse stage re-runs validation) |
| unknown code | no | This file could not be processed. |
| `document_deleted` | n/a | job-only code; never shown |

Whether a failed version offers "Try again" comes from the status route's `retryable`, not from this table (Q4).

## (e) Loading, empty, error and long-running states

| Situation | Behaviour |
|---|---|
| First load | `Skeleton` rows (the `Table` has `isLoading`); filter selects disabled until `/me/classes` loads |
| Empty | three distinct empty states (b1) |
| Error loading list | `Alert` with the mapped message and "Try again" (refetch); the previous data stays visible if there is any |
| Schema mismatch | the existing "unexpected format" message; no partial rendering |
| Load more | button with spinner; an error keeps the loaded pages and shows a retry under the list |
| Mutation in flight | the clicked button shows `isLoading`, the other actions of that row are disabled; no double submit |
| After a mutation | invalidate the list and the document; optimistic updates are **not** used (status is server-driven) |
| 404 on a detail page | the existing `NotFoundState` (same for "not found" and "not yours") |

### Polling (processing, ready, failed)
- **List page:** refetch the list every 5 s **only while** some visible document's latest version is `queued` or `processing` (one request, no N+1 status calls). Stop when none is. `refetchIntervalInBackground: false` (hidden tab does not poll); refetch once when the tab becomes visible again.
- **Detail page:** poll `GET .../versions/{vid}/status` for each in-flight version: 2 s, then 3 s, 5 s, capped at 10 s (a pure `nextPollDelay(attempt)`); a failed poll doubles the wait up to 30 s and shows "Connection lost, retrying" while keeping the last state. Stop on `ready`, `published`, `failed`, `archived`. On `401` the client refreshes once; on `403` or `404` stop and show the mapped message (a 404 means the document was removed). When the status reaches a terminal state, refetch the document once and announce it.
- **Long-running:** show stage text and elapsed time since `created_at`, plus "Large files can take several minutes. You can leave this page; we keep working." After 15 minutes: "Taking longer than usual" (informational, not an error). A version `queued` for more than 5 minutes: "Waiting for the processing service. If this does not change soon, contact your administrator." (nothing is wrong on the client side). The hard stop is 65 minutes (the job ceiling is 60): polling stops and a manual "Check again" button appears.
- Announcements: an `aria-live="polite"` region says "Chapter 4 notes: ready to publish" or "failed: <reason>" when a status changes (section (f)).

### Upload specifics
- Per-request `timeout` of 10 minutes for the POST (the default 15 s would cut a 50 MB upload), `onUploadProgress` for the bar, `AbortController` for Cancel.
- The request guard answers `401`, `413` and `411` **before it reads the body**. A browser that is still sending a big body can then report a network error instead of the status. Mitigation in 5f-3: check size and type before sending, and refresh the access token first when its `exp` has fewer than 60 s left (a small helper that reads the claim already decoded in `AuthContext`). If a network error still happens during upload, the message says "The upload did not finish. Check your connection and try again" and never claims the file was saved.
- Never auto-retry an upload (rate limits count attempts). The interceptor's single replay after a 401 is safe because the guard rejected the first request before processing it.

## (f) Accessibility and mobile behaviour

- Status is never colour only: chip text plus an icon with `aria-hidden`.
- A visually hidden `aria-live="polite"` region for status changes and for "N results" after a search; `role="alert"` on error `Alert`s; focus moves to the first error summary after a failed submit.
- Dialogs use the existing `Modal` (focus trap, Escape, focus return, scroll lock). The publish dialog's checkbox has a real `<label>`; the Publish button is `disabled` and has `aria-describedby` pointing at why.
- **`FileUpload` fix (F8):** the drop zone becomes a real `<button>` (or the input is visually hidden but focusable with a `<label>`), the remove button gets `aria-label="Remove file"`, the `id` comes from `useId`, and the helper and error text are linked with `aria-describedby`. Fixed once in 5f-3; the timetable import benefits too, so its tests must stay green.
- Upload progress is `<progress>` with a text label, not a styled `div` (F9). The usage meters likewise.
- Untrusted text (titles, file names, headings, excerpts, chunk text) is plain text, wrapped (`break-words`), truncated with a visible "Show more" button (not a hover tooltip only).
- Mobile first: the list is the existing stacked card layout; filters collapse into a "Filters" sheet below `sm`; action buttons are at least 44 px high (the Button size and `min-h-[44px]` pattern already used); dialogs are bottom sheets (the `Modal` already is); the flags and hit lists are cards, never wide tables; no horizontal scroll at 360 px; long file names truncate in the middle with the full name in the detail page.
- Keyboard: every action reachable by Tab and operable with Enter/Space; no action only on hover; "Load more" keeps focus on the first new item.
- Colours only through semantic Tailwind tokens (no hex; `check:styles`).

## (g) Test plan

### Decision needed first: how to test components (Q1)
Today only `.ts` logic can be tested (F1). So the plan has two layers, and the second needs your approval:
1. **Logic tests, no new dependency (default).** Put every decision in a pure `.ts` module (policy, error mapper, status chips, polling schedule, file checks, flag/hit view models, request builders, schemas). Components become thin renderers. The API layer is built by a factory `createDocumentsApi(client)` that takes the axios instance as an argument, so tests pass an axios instance with a fake `adapter` (it needs no `import.meta.env`).
2. **Component tests (recommended, adds dev dependencies).** Vitest + jsdom + `@testing-library/react` + `@testing-library/user-event` (devDependencies only, nothing ships to users; `npm audit` and `pip-audit`-style review apply; CI gets one more command). They render the real components with an injected fake `documentsApi` and the real `usePermission` fed by a test `AuthProvider`. If you decline, the same checks are done by layer 1 plus the Playwright run, and the "UI never shows a hidden action" test is the policy matrix plus the source guards below.

### Required tests (one test per rule)
| Area | Tests |
|---|---|
| Schemas (`documentSchemas`) | A full valid payload for every response (document, list, version, status, flags with `matches`, test search with `keyword_rank: null`, usage); every key present with null; unknown extra key accepted (lenient, so a new backend field does not blank the page); unknown `status` string maps to the "Unknown" chip, not a crash; a missing required key throws `SchemaMismatchError` |
| Error mapper | Table-driven over **every code in section (d)** with its HTTP status: message non-empty, does not contain the raw code, the raw server text or a file name; documents never show timetable words ("CSV", "2 MB", "timetable"); 429 uses `Retry-After` header or `retry_after_seconds`; `flags_acknowledgement_required` yields the right wording for `flagged` / `not_scanned`; unknown code falls back per status; network and schema mismatch; version failure table incl. unknown code; `pdf_embedded_file` message equals the backend's fixed text |
| Policy (one test per role rule) | Roles: tenant admin, teacher (full pairs), teacher with `can_manage: false`, custom role with only `document:view`, student, signed-out. States: no version, queued, processing, ready with flags `0` / `>0` / `null`, published, failed retryable, failed permanent, archived. Assert per action `visible`, `enabled`, `reason`. Rules: no permission means hidden; `can_manage` false means disabled with the lock reason; publish only for `ready`; delete version only for `ready`/`failed`; new version disabled while queued/processing/ready; student gets no action at all; admin gets all (via `*`) |
| **UI never shows a hidden action** | (1) Exhaustive loop over the role x state matrix: every action with `visible: false` is absent from `renderActionList()` output, and every rendered action is visible. (2) **Source guard** (like `check-styles`): under `features/documents` no component imports a mutating function of `documentsApi` or calls `can(`/`hasRole(` directly; mutations live only in `hooks/useDocumentMutations.ts`, which is reachable only through policy actions. (3) With layer 2: render the list for each role and assert the DOM has no button named like a hidden action |
| Student isolation | Source test: no file under `features/student` imports `features/documents`; the sidebar config for the student section has no materials item; route table check that every `/teacher/materials*` and `/admin/materials*` route is wrapped in a guard (reads `App.tsx` as text) |
| Request builders | Upload: multipart field names `file`, `title`, `subject_id`, **repeated** `class_ids`, no tenant field; new version: only `file`; list: `class_id`, `subject_id`, `status`, `cursor`, `limit`; flags: `after_ordinal`, `limit`; publish: body `{}` or none when not acknowledged, `{acknowledge_flags: true}` only when ticked, and never a string; test search: only `query`, optional `class_id`, `subject_id`, `include_drafts`, `k` (1-10), never `tenant_id`; usage: GET only |
| Polling | `nextPollDelay` sequence and caps; stops on every terminal status; backoff after errors; the 65-minute stop; `shouldPollList(items)` true only with queued/processing; no poll when hidden (flag passed in) |
| File checks | extensions accepted and rejected (`.xlsx`, `.csv`, `.exe`, none); case-insensitive; empty; 50 MB boundary (exactly 50 MB passes); name sanitising keeps bidi/control characters out of the display name |
| Untrusted text | Source guard: no `dangerouslySetInnerHTML` anywhere in `src` (new ESLint `no-restricted-syntax` rule, added in 5f-1, so `npm run lint` fails on it); with layer 2: a title, heading and chunk text containing `<img src=x onerror=...>` render as literal text |
| Status chips | Table test for every status/stage/flag-count case of b1, including text for "Draft, not checked yet" |
| Test-search view model | `keyword_rank` null hides the keyword line; null similarity shows "-"; `threshold_calibrated` false shows the warning, true the confirmation; `sufficient: false` still lists hits with the note; draft hits are labelled |
| Hardening (5f-10) | Playwright (local only, `world` fixture): student has no Materials entry and gets the 403 page on `/teacher/materials`; teacher uploads a small TXT and sees Waiting/processing; admin sees the usage page; backend-down shows a friendly error |
| Drift guard (optional, Q5) | A test reads `docs/api/phase5.md` and fails when a backticked error code in its code tables has neither a mapping nor an entry in an explicit "intentionally generic" list |

Backend is not changed, so `run_tests gate`, `ruff` and `mypy` are not part of a frontend step (they stay in CLAUDE.md for backend steps).

### Exact commands (all from `frontend/`)
```
npm run typecheck      # tsc --noEmit
npm run lint           # eslint src && node scripts/check-styles.js
npm test               # node --test --experimental-strip-types <explicit file list in package.json>
npm run build          # tsc && vite build
node --test --experimental-strip-types src/features/documents/__tests__/<file>.test.ts   # one file
npm run e2e            # Playwright; local stack only; NOT part of CI (CLAUDE.md says test:e2e; the script is named e2e)
```
Every new test file must be added to the `test` script's list in `package.json` (a file that is not listed never runs). CI runs `npm ci`, `npm run lint`, `npm test -- --run`, `npm run typecheck`, `npm run build`. Each step report quotes: the `# tests / # pass / # fail` lines of `npm test`, the `check:styles` line, the build's last line, and that `tsc --noEmit` printed nothing.

### Baseline today (run 2026-11-20, nothing fixed)
| Command | Result |
|---|---|
| `npm run typecheck` | `tsc --noEmit` printed nothing, exit 0 (about 5 s) |
| `npm run lint` | eslint printed nothing (no warnings), then `✅ Style check passed: 0 inline styles, 0 direct DOM style mutations, 0 disallowed hex colors.`, exit 0 |
| `npm test` | `# tests 128`, `# suites 14`, `# pass 128`, `# fail 0`, `# cancelled 0`, `# skipped 0`, `# duration_ms 346.4622`, exit 0 |
| `npm run build` | `✓ 1799 modules transformed.` ... `dist/assets/index-R3yLIJFq.js 680.53 kB │ gzip: 188.69 kB` ... `✓ built in 4.55s`, exit 0, with the warning "Some chunks are larger than 500 kB after minification" (existing) |

`npm run build` rewrote `frontend/dist`, which is git-ignored (`.gitignore:35`); `git status` still shows only the `.gitignore` change that was there before.

## (h) Steps (each independently shippable; each ends with typecheck, lint, test, build green and nothing committed until you say so)

Order: the read-only list first, then live status, then the writes from the least to the most risky, then search and usage.

### 5f-1: Read-only list with status chips
Builds: `documentSchemas.ts` (list, document, version), `createDocumentsApi` with `list` and `get`, `documentErrors.ts` (the **complete** table of section (d), so later steps only call it), `statusChip.ts`, `documentPolicy.ts` (view actions only), `useDocuments` (infinite query), `MaterialsListPage` for `/teacher/materials` and `/admin/materials`, sidebar items, replace the teacher class placeholder with a link, ESLint `no-restricted-syntax` ban on `dangerouslySetInnerHTML`, shared mapper gets `pdf_embedded_file` and `subject_has_documents` (B82, B20; check which pages use `getFriendlyError` for subject delete).
Acceptance:
1. A teacher sees the list for their classes; an admin sees all; both can filter by class, subject, status; filters survive reload (URL).
2. Every row shows the right chip for every status/stage/flag-count case (tested table); unknown status shows "Unknown".
3. "Load more" follows `next_cursor`; an error keeps loaded rows.
4. Loading, empty (three kinds), error and schema-mismatch states exist.
5. No upload/delete/retry/publish control appears anywhere (read-only step).
6. A student has no sidebar item and gets the 403 page on the URL.
7. Error mapper: every code of (d) tested; documents never show timetable wording.
8. Untrusted text rendered as plain text; the ESLint rule fails on a deliberate test violation.
9. New tests added to the `package.json` list; baseline tests still 128/128 plus the new ones.

### 5f-2: Document detail and live status
Builds: detail page with the versions table, `status` API call, `nextPollDelay`, `useVersionStatus`, list auto-refresh while in flight, long-running and stuck-queued messages, `getVersionFailure` rendering, `aria-live` announcements.
Acceptance: 1. a processing version updates itself to ready/failed without a reload; 2. polling stops on terminal states, when the tab is hidden, and at 65 minutes; 3. a lost connection keeps the last state and says so; 4. a failed version shows its plain reason (every code of the table tested); 5. 404 shows `NotFoundState`; 6. still read-only; 7. polling logic tested without timers (pure functions).

### 5f-3: Upload a new document
Builds: upload dialog, client file checks, `FileUpload` a11y fix, progress and cancel, per-request timeout, pre-upload token freshness, field-level errors, `duplicate_content` notice, `duplicate_version` link.
Acceptance: 1. only users with `document:upload` see the button; 2. the class list is only the classes offering the chosen subject within the user's pairs; 3. wrong type, empty and over-50-MB files are refused before sending with plain text; 4. repeated `class_ids` and the other fields are sent exactly (request-builder test); 5. progress, cancel and no double submit; 6. each upload error code of (d) shows its message in the right place and the form keeps its values; 7. 202 closes the dialog, shows a toast and the new row as Waiting; 8. a network drop mid-upload shows the "did not finish" text; 9. timetable import tests still pass after the `FileUpload` change.

### 5f-4: Retry a failed version and delete a version
Builds: status-based retry (needs `retryable`), `ConfirmDialog` for version delete, `useDocumentMutations` (the only place mutations live), the source guard.
Acceptance: 1. Retry shows only for failed + retryable versions the user may change; 2. each retry code (409 x4, 429 x2, 503) shows its message with the wait text; 3. 202 returns the version to Waiting and polling resumes; 4. version delete only for `ready`/`failed`, with a named confirmation; `version_not_deletable` handled; 5. `can_manage` false shows the lock reason; 6. the hidden-action matrix test and the source guard pass.

### 5f-5: New version and delete document
Builds: the upload dialog in "new version" mode, document delete confirm, `document_processing_in_progress` guidance.
Acceptance: 1. New version is disabled with "Publish or delete the draft first" while a version is queued/processing/ready; 2. only `file` is sent; 3. `duplicate_version` and `document_processing_in_progress` handled; 4. document delete names the document, needs `document:delete` and `can_manage`, returns to the list; a 404 on delete counts as already removed; 5. the old published version stays visible as "Published" until the new one is published (list shows both chips).

### 5f-6: Flags viewer (read-only)
Builds: flags API with paging, the Flags tab, code descriptions, `matches` rendering, title/file-name flag notes, the "not checked yet" wording.
Acceptance: 1. paged list with "Load more" using `next_after_ordinal`; 2. `null` count, `0` and `>0` each have their own text; 3. a stored code without a current match says so; 4. page range and heading path shown; 5. all text is plain text (literal `<script>` test); 6. available for `ready` and `published` versions, 404 handled; 7. no publish control yet.

### 5f-7: Publish with flag review
Builds: publish dialog (simple and review variants), required acknowledgement checkbox, 409 `reason` handling, publish API.
Acceptance: 1. `acknowledge_flags: true` is sent only when the box is ticked and only for flagged or never-scanned versions; 2. the dialog shows the count and first flags; 3. a late `flags_acknowledgement_required` opens the review variant with the right wording for `flagged` / `not_scanned`; 4. `version_not_ready`, `publish_conflict`, 403, 404 each reload and explain; 5. Publish is hidden without `document:publish`, disabled with the lock reason when `can_manage` is false, absent unless the version is `ready`; 6. after success the chip becomes Published and the older version becomes "Older version"; 7. a non-boolean can never be sent (builder test).

### 5f-8: Test-search panel
Builds: search API, panel on the detail page and the list page, result view model, error handling.
Acceptance: 1. Only the allowed fields are sent; 2. `keyword_rank: null` hides keyword info; `vector_similarity` shown to three decimals; 3. threshold and `threshold_calibrated` shown as in b8; 4. `sufficient: false` shows the banner and still lists hits, with the "assistant would not use them" note; 5. "Include drafts" off by default and drafts labelled; 6. `invalid_query`, `query_too_long`, 429 (with wait), each 503 code and 404 handled; 7. chunk text is plain text and collapsible; 8. the query is not stored or put in the URL; 9. the slow first search shows the loading hint.

### 5f-9: Admin usage
Builds: usage API, usage page/tab, native `<progress>` meters, limits table.
Acceptance: 1. visible only with `settings:manage` (hidden for a teacher; 403 handled); 2. three meters with numbers and percent, amber and red thresholds with words; 3. limits table with human units; 4. loading/error states; 5. no purge control.

### 5f-10: Hardening and release notes
Builds: Playwright flow (student isolation, teacher upload to Waiting, admin usage), 360 px and keyboard walkthrough checklist, optional drift guard (Q5), removal or rewording of stale placeholders (Q8), README/docs update (CLAUDE.md line for the e2e script name, once you allow edits to it), `docs/phase5f-plan.md` build record.
Acceptance: the e2e flow passes locally; the checklist of section (f) is ticked in the record; no placeholder claims a feature that is not there.

### Deferred, not planned in 5f
- 5f-11 optional: download the original (authenticated blob download, `Content-Disposition` name ignored, our own sanitized name, 10-minute timeout, `404` also when the file is missing). Small; included only if you want it (Q9).
- Rename and relink classes (`PATCH`), chunk exclude (B94, no route), purge (no list of deleted documents), per-tenant limits for teachers (Q3), B98 (title edited after publish), the student view of materials (Phase 6/8).

## (i) Open questions for you

| # | Question | My recommendation |
|---|---|---|
| Q1 | **Component tests:** may I add dev dependencies Vitest, jsdom and Testing Library (layer 2 of section (g))? Without them there is no way to render a component in a test, only Playwright (manual, not in CI). | Yes. They are devDependencies only; keep `npm test` for logic and add `npm run test:components`, both in CI. |
| Q2 | **CLAUDE.md** says not to edit `frontend/` and names `test:e2e`. May frontend work start (and which agent or owner may edit `frontend/`)? May I fix the `test:e2e` line? | Allow `frontend/` for 5f only and fix the line (it is wrong today). |
| Q3 | **Teachers cannot see their tenant's file-size and quota limits** (usage needs `settings:manage`). Add a small read-only backend route (for example `GET /documents/limits`, `document:view`, effective limits only) before 5f-3? Without it the upload dialog cannot warn before sending, and the 413 message cannot name a size. | Yes, a tiny backend step before 5f-3 (its own tests; no frontend work blocked until then). |
| Q4 | **`retryable` is missing from list items.** Use the status route for each failed version (an extra GET per failed version on the detail page; no backend change), or add `retryable` to `DocumentVersionResponse`? | Status route in 5f-4; add the field only if the extra call proves clumsy. |
| Q5 | **Drift guard** (a test that reads `docs/api/phase5.md` and fails on an unmapped code): a cross-folder test file. Want it? | Yes, in 5f-10, with an explicit allow-list. |
| Q6 | **Wording.** The messages in section (d) are drafts. Do you want to review the full table before 5f-1, and is English only enough (the platform serves Hindi and Marathi content; the UI language is English today)? | English only for 5f; you review the table when 5f-1 is built (the mapper is one file). |
| Q7 | **Custom roles.** A user who is neither teacher nor admin but holds `document:view` (custom role) has no portal and an empty backend scope. Confirm this is out of scope for v1. | Out of scope. |
| Q8 | **Student placeholders.** `StudentClassDetailPage` says instructor uploads "will appear here" and `DashboardPage` says documents come in Phase 5. Students see no materials in 5f. Reword both to the Phase 6 chat only, or leave until Phase 6? | Reword in 5f-10 so nothing promises a student document list. |
| Q9 | **Scope extras:** download of the original, rename, relink classes. Want any of them in 5f (download as 5f-11)? | Download yes (teachers expect to re-open their file); rename and relink later. |
| Q10 | **Where it lives in the menu:** one "Course materials" entry per portal as planned, or inside each class page only? | One entry per portal plus a link from the class page. |
| Q11 | **Test-search access:** admins and teachers both see "Include drafts". Any reason to hide drafts from admins? | No; same for both. |
| Q12 | **Unscanned versions.** The UI tells teachers "not checked yet, ask an administrator to run a rescan" for `flagged_chunk_count` null. Is that the wording you want, given the rescan is an operator CLI today? | Keep, and drop the sentence once no unscanned versions exist (B102). |

## (j) Build record

### Phase 5f-1a: Frontend foundations (2026-11-21)
- Tooling: Vitest + jsdom + @testing-library/react + @testing-library/user-event added as devDependencies; `npm run test:components` added; `vitest.config.ts` created; `component_setup.test.tsx` verified; CI step added in `.github/workflows/ci.yml`.
- CLAUDE.md: updated frontend edit permission for Phase 5f and updated script name `npm run e2e`.
- ESLint: added `no-restricted-syntax` rule forbidding `dangerouslySetInnerHTML` in `src/`; verified with `eslint_dangerously_set_inner_html.test.ts`.
- Schemas: implemented `frontend/src/features/documents/schemas/documentSchemas.ts` with lenient Zod schemas for list, document, version, status, flags, test search, usage, limits, and `validateResponse` / `SchemaMismatchError`. Verified with `documentSchemas.test.ts`.
- API: implemented `frontend/src/features/documents/api/documentsApi.ts` with `createDocumentsApi(client)` (read-only: `list`, `get`, `limits`). Verified with `documentsApi.test.ts` using fake adapter.
- Error mapper: updated `src/api/errorMessages.ts` with `pdf_embedded_file` and `subject_has_documents`; implemented `frontend/src/features/documents/utils/documentErrors.ts` with `getDocumentError` and `getVersionFailure`. Verified with `documentErrors.test.ts`.
- Status chip: implemented `frontend/src/features/documents/utils/statusChip.ts` with `getStatusChip`. Verified with `statusChip.test.ts`.
- Document policy: implemented `frontend/src/features/documents/utils/documentPolicy.ts` with `getDocumentActions`. Verified with `documentPolicy.test.ts`.

### Phase 5f-1a-fix: Corrections after review (2026-11-21)
- Policy: "New version" action always evaluated from `document.latest_version` (not version row); disabled while latest is queued/processing/ready; hidden when no document or no latest_version.
- Policy tests: added full state suite (queued, processing, ready, published, failed, archived), flags (0, >0, null), archived version visibility, retry with can_manage: false, delete version with can_manage: false for ready and failed, signed-out user (all hidden), and admin access through the real `usePermission` hook.
- Error mapper: wait text appended strictly for HTTP 429; formatted >= 60s as minutes rounded up; added full stop to `too_many_concurrent_uploads`; 503 with `Retry-After` does not append a duplicate sentence.
- Error wording: upload 404 says "We could not find the class or subject. Choose one of your own classes."; retry `duplicate_version` says "The same file is already an active version."; quota errors end with "Ask an administrator."; context-passing overrides covered.
- `getVersionFailure`: mapped `xml_too_complex` to "The document could not be processed. Try exporting it again or upload a different format."; restricted mapped codes strictly to version failure table and upload validation codes with fallback for any other code.
- ESLint test: checks rule severity 2 ("error"), tests both JSX attribute and plain identifier/object property selectors, and resolves paths relative to the test file.
- Schemas test: added comprehensive "every key present with null" suite across all response schemas.
- Cleanups: component setup test uses neutral text without tool or product names; package.json trailing newline restored.

### Phase 5f-1b: Read-only course materials list page (2026-11-21)
- Client: created `frontend/src/features/documents/api/documentsClient.ts` providing standard instance of `documentsApi` using `apiClient`.
- Hook: implemented `frontend/src/features/documents/hooks/useDocuments.ts` using `useInfiniteQuery` over `documentsApi.list` (limit 20, opaque cursor); synced filters `class`, `subject`, `status` with URL query parameters (`?class=&subject=&status=`); populated filter options dynamically via `/me/classes` (`academicApi.getMyClasses`).
- Page: implemented `frontend/src/features/documents/pages/MaterialsListPage.tsx` used at `/teacher/materials` and `/admin/materials`. Features:
  - PageHeader "Course materials" with route-aware breadcrumbs.
  - Filter bar for Class, Subject, and Status.
  - Data table with title (plain text, break-words), subject name, class badges (first 3 + count badge), status chip (from `getStatusChip`), dual version badge ("Published: vN"), formatted file size/type, and updated date.
  - `can_manage=false` lock note using plain language from `blocked_reason`.
  - "Load more" button with pending spinner following `next_cursor`.
  - Three distinct empty states: no assigned classes (for teacher), no materials match (with clear filters button), and no materials yet.
  - Error state displaying mapped error with "Try again" button, keeping loaded rows visible.
  - Strictly read-only: no upload, retry, delete, publish, or search controls; no polling; literal rendering for untrusted text.
- Route Guards: updated `TeacherRoute` and `AdminRoute` in `frontend/src/features/auth/guards.tsx` to support `permission?: string` prop and enforce `can(permission)`. Added guarded routes in `App.tsx`.
- Sidebar Navigation: added "Course Materials" in `Sidebar.tsx` under Administration and Teacher Portal with `permission="document:view"`. Kept Student Portal isolated with zero materials links.
- Class Detail Link: replaced "Coming in Phase 5" placeholder in `TeacherClassDetailPage.tsx` with a direct link to `/teacher/materials?class=${classId}`.
- Tests: verified `student_isolation_source.test.ts` (3/3 passing in `npm test`, total 161 node tests) and `MaterialsListPage.test.tsx` (9/9 passing in `npm run test:components`). All linting, style rules, and typechecks green.

### Phase 5f-2: Document detail page and live status (2026-11-21)
- API Client: added `status(documentId, versionId)` to `frontend/src/features/documents/api/documentsApi.ts`. Paths are relative to client `/api/v1` baseURL. Added URL test asserting final URL from `axios.getUri`.
- Pure Polling Schedule (`frontend/src/features/documents/utils/pollingSchedule.ts`):
  - `nextPollDelay(attempt)`: returns backoff in ms (2000, 3000, 5000, capped at 10000); on consecutive poll failures doubles wait up to 30000ms.
  - `isTerminalStatus(status)`: identifies terminal version states (`ready`, `published`, `failed`, `archived`).
  - `shouldPollList(items, isHidden?)`: returns true only when a visible latest version is `queued` or `processing`; false when tab is hidden or list is empty.
  - `getLongRunningStatus(status, stage, createdAt)`: formats stage text with elapsed time, detects queued > 5 min ("Waiting for the processing service. If this does not change soon, contact your administrator."), in-progress > 15 min ("Taking longer than usual"), and hard stop at 65 min ("Check again" prompt).
- Polling Hook (`frontend/src/features/documents/hooks/useVersionStatus.ts`):
  - Polls status for in-flight versions (`queued` / `processing`) with dynamic backoff.
  - Ceases polling on terminal states, hidden tab (`refetchIntervalInBackground: false`), 403/404 errors, or hard stop (65 min).
  - Retains last valid state during network hiccups and displays "Connection lost, retrying".
  - Fires `onTerminalStatus` callback once upon reaching terminal status to trigger document refetch.
- List Page Polling & Links:
  - `useDocuments.ts`: queries refetch every 5000ms only while `shouldPollList(items)` evaluates to true.
  - `MaterialsListPage.tsx`: links title and row clicks to `/teacher/materials/:documentId` and `/admin/materials/:documentId`.
- Detail Page (`frontend/src/features/documents/pages/MaterialDetailPage.tsx`):
  - Registered at `/teacher/materials/:documentId` and `/admin/materials/:documentId`, guarded with `permission="document:view"`.
  - Header renders title, subject, linked classes, and `can_manage=false` lock note.
  - Versions table renders version number, plain text file name (`break-words`), type, size, pages, status chip, uploaded date, and published status.
  - Failed versions display plain reason from `getVersionFailure(error_code)`.
  - HTTP 404 renders `NotFoundState` with link back to materials list.
  - Visually hidden `aria-live="polite"` region announces status changes (`"<title>: ready to publish"` / `"<title>: failed: <reason>"`).
  - Strictly read-only: no upload, retry, delete, publish, flags tab, or search tab.
- Tests & Verification:
  - Unit tests: `pollingSchedule.test.ts` (9 tests), `documentsApi.test.ts` (status endpoint test), `student_isolation_source.test.ts` (detail routes guarded). All 173 node tests passing across 14 test suites in `npm test`.
  - Component tests: `MaterialDetailPage.test.tsx` (7 tests covering live transition, terminal state stop, error display, failure reasons, 404 state, view-only lock note, and XSS safety). All 18 component tests passing across 3 test suites in `npm run test:components`.
  - Full quality checks: `npm run typecheck`, `npm run lint` (0 styles violations), and `npm run build` all pass cleanly with exit code 0.

### Phase 5f-3: Upload a new document (2026-11-21)
- API Client (`frontend/src/features/documents/api/documentsApi.ts`):
  - Added `upload(form, { onProgress, signal })` with a 10-minute (600,000 ms) per-request timeout.
  - Endpoints relative to client `/api/v1` baseURL (`POST /api/v1/documents`).
  - Strict multipart payload builder (`buildUploadFormData`): includes `file`, `title`, `subject_id`, repeated `class_ids` (never comma-separated, never JSON-stringified, no client tenant field). Validates 202 response against `DocumentVersionSchema`.
- File Pre-validation (`frontend/src/features/documents/utils/fileChecks.ts`):
  - Pure functions: `validateDocumentFile(file, maxBytes)` and `sanitizeDisplayFilename(filename, maxLength)`.
  - Allowed extensions: `.pdf`, `.docx`, `.pptx`, `.txt`, `.md` (case-insensitive).
  - Explicit friendly rejection for spreadsheets/CSVs: "Spreadsheets are not accepted here; export it as PDF or DOCX".
  - Empty file refusal: "The file is empty.".
  - Disallowed file formats refusal: "Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets and CSV are not accepted.".
  - Tenant size limit enforcement (fallback to 50 MB DEFAULT_MAX_FILE_BYTES) with 50 MB boundary validation: "This file is larger than your organisation allows. Make it smaller or split it.".
  - `sanitizeDisplayFilename`: strips Windows/Unix path separators, ASCII/Unicode control characters, and bidi override characters (e.g. \u202E RLO).
- FileUpload Accessibility Fix (F8) (`frontend/src/components/ui/FileUpload.tsx`):
  - Converted drop zone outer wrapper into a keyboard-focusable `<button type="button">`.
  - Added `aria-label="Remove file"` to remove button.
  - Connected input and button with `useId()` and linked helper / error descriptions with `aria-describedby`.
  - Preserved backward compatibility: Timetable import wizard tests remain 100% green (`import_wizard.test.ts`: 24/24 passing).
- Central Mutation Hook (`frontend/src/features/documents/hooks/useDocumentMutations.ts`):
  - Implemented `useUploadDocument` via TanStack `useMutation`.
  - Pre-upload token freshness guard (`isTokenExpiringSoon`): automatically refreshes JWT if expiration is within 60 seconds (`authApi.refreshToken`).
  - Zero auto-retries on upload failure (`retry: false`).
  - Network error handling in `documentErrors.ts`: maps upload drop to "The upload did not finish. Check your connection and try again".
- Upload Dialog Component (`frontend/src/features/documents/components/UploadDialog.tsx`):
  - Accessible modal dialog for document upload with focus trapped once on open without stealing focus on input keystrokes (`useFocusTrap` ref stabilization).
  - Title input with 1-200 character validation and dynamic counter (`${title.length} / 200`).
  - Tenant limits (`useDocumentLimits` fetching `GET /documents/limits`): dynamic helper text (`Max ${maxMb} MB`), client file size/format checks, with 50 MB fallback when loading/failed.
  - Subject dropdown populated from teacher's assigned subjects via `/me/classes` (`academicApi.getMyClasses`).
  - Classes multi-select checkboxes dynamically filtered to classes offering the selected subject; restricts selection to 1-50 classes.
  - Native `<progress>` bar indicating upload percentage with accessible text label.
  - Cancel button with `AbortController` signal to abort in-flight uploads.
  - Form field errors displayed inline (`invalid_title`, `too_many_classes`, file errors); preserves form input values and selected file on submission errors; automatically focuses the first invalid field on failed submit.
  - Conflict handling: `duplicate_version` error displays plain mapped message without invented document link.
  - Warning notification: displays informative toast if backend returns `duplicate_content` warning in 202 response.
- Materials List Integration (`frontend/src/features/documents/pages/MaterialsListPage.tsx`):
  - Integrated "Upload material" button in PageHeader; visibility strictly controlled by `documentPolicy` (`canUpload = policyActions.find(a => a.id === "upload")?.visible`).
  - Opens `UploadDialog`; invalidates documents queries upon successful upload.
  - Portal route resolution factored into pure `roleUtils` (`getMaterialsBasePath`).
- Architectural Guard (`frontend/src/features/documents/__tests__/source_guard.test.ts`):
  - Pure function `findViolations(source, fileName)` scanning every `.ts`/`.tsx` file under `features/documents`. Verified with injected bad snippets (`can("x")`, `hasRole(`, `.roles.includes(`, `.permissions.includes(`, `documentsApi.upload(`, `api.upload(`, mutating calls, `import ... buildUploadFormData`) and clean snippets.
- Tests & Verification:
  - Unit tests: `fileChecks.test.ts` (8 tests), `documentsApi.test.ts` (upload and `buildUploadFormData` tests), `source_guard.test.ts` (findViolations pure unit tests + directory scan), `roleUtils.test.ts` (4 tests). All 190 node tests passing across 14 suites in `npm test`.
  - Component tests: `UploadDialog.test.tsx` (21 tests), `FileUpload.test.tsx` (5 tests), `MaterialsListPage.test.tsx` (9 tests), `MaterialDetailPage.test.tsx` (7 tests), `component_setup.test.tsx` (2 tests). All 44 component tests passing across 5 suites in `npm run test:components`.
  - Full suite check: `npm run typecheck`, `npm run lint` (0 style violations), and `npm run build` all pass cleanly with exit code 0.





### Phase 5f-4: Retry a failed version and delete a version (2026-11-21)
- API (`documentsApi.ts`): `retryVersion` (`POST /documents/:id/versions/:vid/retry`, 202, validated as `DocumentVersionSchema`) and `deleteVersion` (`DELETE /documents/:id/versions/:vid`, 204). Tested by final URL and method (`axios.getUri`). The existing "every API function" guard test now also checks that each function really sends a request (it passed vacuously before because the request counter was cumulative, and `upload` never sent one).
- Mutations (`hooks/useDocumentMutations.ts`, the only place): `useRetryVersion`, `useDeleteVersion`. `retry: false`, invalidate list and document, no optimistic update. A 404 on delete counts as "already removed". A 403 `document_manage_requires_all_classes` refetches the document.
- Decision (differs from plan b4 text): "Try again" is decided from `retryable` on the version response, not from the status route. The status route is not called for this.
- Policy (`documentPolicy.ts`): retry and delete rows were already there; added `getFailedVersionHint` ("Upload a corrected file as a new version", only for failed + `retryable === false` and users who may upload). Components render only what the policy returns.
- UI: `components/VersionRowActions.tsx` (card footer actions, full width and `min-h-[44px]` on mobile, Delete in its own bordered block), page-level `Alert` for retry errors, `ConfirmDialog` for delete (names the version, file name sanitised and truncated, error shown inside the dialog, Cancel focused first, confirm disabled when the policy no longer allows it). Detail page default layout is now `stacked` (cards on phones).
- Polling fix: `useVersionStatus` remembered a terminal state, so a retried version would have stayed "Failed" and never been polled again. It now forgets it when the document says the version is back in flight. A retried version also gets its polling clock from the click, not from the upload (otherwise an old version would be past the 65 minute stop immediately).
- Shared component additions (optional props, nothing changes for existing callers): `Modal`/`useFocusTrap` `initialFocusRef`, `ConfirmDialog` `error`, `confirmDisabled`, `initialFocus="cancel"` and `min-h-[44px]` buttons, `Table` `mobileActionsLayout`, `Toast` `useOptionalToast`.
- Tests: `VersionActions.test.tsx` (33 component tests: every retry code, delete codes, named confirmation, Cancel/Escape/backdrop, double submit incl. same-tick, 404, policy gating, student sees nothing, 44px classes, keyboard, polling resume incl. an old version), policy matrix rows, API URL tests, guard coverage of the new API functions.

### Phase 5f-5: New version and delete document (2026-11-21)
- API (`documentsApi.ts`): `uploadVersion(documentId, form | {file}, {onProgress, signal})` is `POST /documents/:id/versions` (multipart, 10 minute timeout, abortable, response validated as `DocumentVersionSchema`); the real builder `buildNewVersionFormData` appends only `file`. `deleteDocument(documentId)` is `DELETE /documents/:id` (204). Both upload routes share one request config (`multipartConfig`). Tested by final URL and method (`axios.getUri`); the "every API function" guard now sends a real request for the new functions and requires both names.
- Mutations (`hooks/useDocumentMutations.ts`, still the only place): `useUploadVersion` (token freshness first, `retry: false`, refetches list and document; also after 409 `document_processing_in_progress` and 403 `document_manage_requires_all_classes`) and `useDeleteDocument` (404 counts as "already removed"; 403 manage refetches and is not retried; on success the list is refetched and the document is removed from the cache, because a refetch would only answer 404).
- UploadDialog `mode="version"` (+ `documentId`, `onShowVersion`): only the file field; title, subject and classes are hidden and not validated, the class list is not loaded. Same client checks, progress, Cancel, token freshness and error placement as the new-document mode (file codes beside the file zone, everything else in the dialog alert). `duplicate_version` shows the fixed message and a "Show it" control (only when the response carries `version_id`); it closes the dialog, scrolls to and focuses that version's row on the same page (`data-version-row`, the visible one of card and table). `document_processing_in_progress` shows the guidance text. New context `"new_version"` in `getDocumentError` (a 404 says "material", not "class or subject").
- Detail page: `components/DocumentActions.tsx` renders the policy actions `new_version` and `delete_document` (full width and `min-h-[44px]` on phones, Delete in its own bordered block, a disabled action shows its reason as text linked by `aria-describedby`). Delete document: `ConfirmDialog` "Delete 'title'?" + "Students will no longer see it and it cannot be restored from here.", Cancel focused first, bottom sheet on phones, toast and back to the materials list; the title is cleaned with the new `sanitizeDisplayText` (bidi and control characters, slashes kept). No component calls `can(` or `hasRole(`.
- Leftover A: `MaterialsListPage` defaults to the stacked card layout; each card has an "Open" link in the footer (policy action `view`, `min-h-[44px]`, full width on phones). The wide table is only in the DOM inside a `hidden md:block` wrapper. Existing list tests now look into the card list.
- Leftover B: the Versions row shows the file name through `sanitizeDisplayFilename(name, 255)` with `break-words`. Side effect: path parts are removed, so a name such as `<script>x</script>.pdf` is shown as `script>.pdf` (one existing XSS-literal test now uses a name without a slash).
- Leftover C (known limitation, not fixed): `DocumentVersionResponse` carries only `created_at` and `published_at`; there is no updated or processing-started timestamp (the status route has `updated_at`, but it is only called while polling, and the 65 minute stop is evaluated before the first poll). After a page reload a version retried hours after upload therefore still shows "Check again" instead of polling, and nothing is invented. A tripwire test in `documentSchemas.test.ts` fails when the backend adds such a field, as the reminder to use it. Fix needs a backend field (for example `updated_at` or `processing_started_at` on the version response).
- Open question for the owner: a document whose only version was deleted has `latest_version: null`, and the existing policy (pinned by tests) hides "New version" then, so the only way out is to delete the document.
- Tests: node `documentsApi` (real builder, final URLs, guard), `fileChecks`, `documentErrors` (new_version context), `documentPolicy` (role x can_manage x status matrix for new version and delete document), `documentSchemas` (tripwire), `source_guard`; component `NewVersionAndDelete.test.tsx` (dialog mode, detail actions, delete document, bidi file name) and `MaterialsListPage.test.tsx` (stacked layout). Ten rules were switched off one at a time to prove a test goes red (builder extra field, 404 as removed, same-tick double submit, file name sanitising, Cancel focus, list layout, Show it focus, version-mode title validation, can_manage lock, delete visibility); each time a test failed, then the code was restored.

### Phase 5f-6: Flags viewer (read-only) (2026-11-21)
- API (`documentsApi.ts`): `flags(documentId, versionId, options)` is `GET /documents/:id/versions/:vid/flags` (default `limit: 50`, `after_ordinal`, response validated as `VersionFlagsSchema`). Tested by final URL and method (`axios.getUri`); the "every API function" guard covers `flags`.
- Schemas (`documentSchemas.ts`): `VersionFlagsSchema` accepts `matches` array per item and unknown extra keys (`.passthrough()`).
- Hook (`hooks/useVersionFlags.ts`): `useVersionFlags(documentId, versionId)` uses `useInfiniteQuery` with cursor paging on `next_after_ordinal`. Aggregates all pages into flat `items`, preserves `firstPage` metadata (`flagged_chunk_count`, `title_flagged`, `filename_flagged`), exposes `hasNextPage`, `fetchNextPage`, `isFetchingNextPage`.
- Detail page integration (`pages/MaterialDetailPage.tsx`):
  - Uses `Tabs` component with "Versions" and "Flags" tabs.
  - "Flags" tab trigger visible and enabled when a version is `ready` or `published` via `documentPolicy` action `flags`.
  - Switching versions or clicking "Review flags" updates `selectedFlagsVersionId` and switches active tab to "flags".
  - Versions row action: "Review flags" button rendered in `VersionRowActions.tsx` when `documentPolicy` allows `review_flags` (visible for ready/published versions with `flagged_chunk_count > 0` or `null`). Button is at least 44 px high (`min-h-[44px]`).
- Flags viewer component (`components/FlagsViewer.tsx`):
  - Tripwire alert: "This check is a tripwire. Text that is not flagged is not guaranteed safe."
  - Heading texts based on `flagged_chunk_count`:
    - `> 0`: "N passages were flagged for review"
    - `0`: "Nothing was flagged"
    - `null`: "This file has not been checked yet. You can still publish after you confirm you have read this notice."
  - Metadata alerts when `title_flagged` ("The title of this material contains flagged text.") or `filename_flagged` ("The original file name contains flagged text.").
  - Stacked cards for items (never a wide table):
    - Page range: "Page 3" / "Pages 3-4" with label "Page/slide".
    - Heading path rendered as plain React text (`break-words`).
    - Injection code chips with plain English descriptions (`FLAG_CODE_DESCRIPTIONS` in `utils/flagDescriptions.ts`, e.g. instruction_override: "Tells an AI to ignore its instructions", unknown code fallback: "Flagged for review"). Chips wrap on narrow screens.
    - Matched spans: `matches[].source` ("in the heading" / "in the text") and `matches[].excerpt`. Empty matches fallback: renders chunk excerpt and displays "No longer matches the current rules".
    - "Excluded" badge marker shown when `excluded` is true.
    - Long excerpts wrap with `break-words` and feature a visible "Show more" / "Show less" toggle button (`min-h-[44px]`, not a tooltip).
  - Error states via `getDocumentError`: 404 ("We could not find this material. It may have been removed.") and 403 ("You do not have permission to do this.").
  - Next-page load error keeps loaded items visible with inline alert and "Try again" button.
  - Loading skeleton state and empty states.
  - Untrusted text safety: all text from the backend (heading, excerpts, file names) is rendered as plain React text with `whitespace-pre-wrap break-words`, never HTML.
  - Mobile (360px): full width stacked cards, >= 44px min-height buttons ("Review flags", "Load more", "Show more", "Try again"), zero horizontal scroll, keyboard reachable (Tab, Enter).
- Tests:
  - Node tests: `flagDescriptions.test.ts` (table-driven codes, count headings, page ranges, match sources, constants), `documentPolicy.test.ts` (review_flags and flags tab policy matrix), `documentsApi.test.ts` (flags endpoint, default limit 50, after_ordinal, schema validation, guard coverage), `documentSchemas.test.ts` (matches and extra keys). Added `flagDescriptions.test.ts` to `test` script in `package.json`.
  - Component tests: `FlagsViewer.test.tsx` (15 tests: detail page integration, tab switching, headings, title/filename notes, page ranges, code descriptions, matched spans, empty matches fallback, excluded marker, XSS plain text rendering for script and img onerror, 404 and 403 error states, next page error keeping items, mobile Show more toggle, keyboard navigation, student isolation).
  - Source guard (`source_guard.test.ts`): verified clean across all documents files with zero violations.

### 5f-7: Publish with flag review (built, not committed)
- **API:** `documentsApi.publishVersion(documentId, versionId, { acknowledgeFlags })` is `POST /documents/:d/versions/:v/publish`. The body comes only from `buildPublishBody(acknowledge)`: `{ acknowledge_flags: true }` for exactly the boolean `true`, otherwise `{}`.
- **Dialog (`components/PublishDialog.tsx`):** simple confirm only when `flagged_chunk_count === 0` (`needsFlagReview` in `utils/publishReview.ts`); review variant for `> 0`, `null`, or after a server `flags_acknowledgement_required`. The review variant loads the flags (`useVersionFlags`, first page), shows count, cards, title/file-name warnings (`FlagSourceNotices`, now shared with the Flags tab), a "Review all N" button (closes the dialog and opens the Flags tab) and the required, never pre-ticked checkbox. Publish is also off while the flags load or failed to load. The dialog is mounted only while open and keyed by version id, so reopening or a version change resets it.
- **Hook:** `usePublishVersion` in `hooks/useDocumentMutations.ts` (retry off; reloads the document after version_not_ready, publish_conflict, flags_acknowledgement_required, 403 manage, 404).
- **Page:** Publish button in `VersionRowActions` (from `documentPolicy`). After a publish the replaced published version is remembered locally and shown as "Older version" (the document payload no longer lists archived versions). Fixed a latent bug: the polled status overlay now applies only while the document says the version is queued/processing, so a remembered "ready" cannot hide a later publish.
- **Tests:** 5 new node tests (builder, URL/method/body, guard) and 37 component tests (`PublishDialog.test.tsx`). Existing API mocks gained `publishVersion`.

### 5f-8: Test-search panel (built, not committed)
- **API:** `documentsApi.search(input, { signal? })` is `POST /documents/test-search`. The body comes only from `buildTestSearchBody(input)`: the question is trimmed and must be 1-500 characters (`SearchInputError` with code `invalid_query` or `query_too_long` before any request), `class_id` / `subject_id` only when non-empty, `include_drafts: true` only for the boolean true, `k` only as an integer 1-10; any other key (for example `tenant_id`) is dropped. The question is never in the URL or params.
- **View model (`utils/searchView.ts`):** banner per `reason` (plan b8), threshold line, `candidate_count` line, calibration text from `threshold_calibrated` (false: "Not calibrated for the current search model (X). Similarity numbers may mislead until retrieval is recalibrated."), hits sorted by `rank`, similarity to three decimals ("-" when null) with "above/below the threshold" (the plan defines no strong/weak bands, so none are shown), Draft / Published chip, keyword line only when `keyword_rank` is not null, fused score and vector rank under "Technical details", `collapsePassage` for "Show more", announcement text, and `describeSearchError` (texts only from the error table; `getSearchInputMessage` added for the client-side input errors).
- **Hooks:** `hooks/useTestSearch.ts` (read-only; plain state, no query cache, no storage, one request at a time, aborts on unmount) and `hooks/useDelayedFlag.ts` (the "Loading the search model" hint after 3 s). No mutation was added to `useDocumentMutations.ts`.
- **Panel (`components/TestSearchPanel.tsx`):** shown only when `documentPolicy` says `test_search` is visible (`document:view`). Question box with counter (soft limit 500), class / subject / number of results selects, "Include drafts" (off by default, with help text saying only published versions are searched otherwise), Search button (full width on a phone, 44 px, disabled and "Searching…" while in flight). Results are stacked cards in an ordered list; every text from the server is rendered as React text with `break-words` (no HTML, no Markdown, no link made from text; the title link uses our own document id). Focus moves to the "Search results" heading after a search (to the box for a 422 about the question, to the error otherwise); a polite live region says "N passages found" or repeats the empty-state text; errors use `role="alert"`.
- **Mounted:** a "Test search" tab on the detail page (subject and the document's classes preset) and on the list page (tabs appear only with the permission; class and subject start from the page filters; filter changes made later apply the next time the tab opens).
- **Tests:** `testSearch.test.ts` (node: builder, final URL and method, view model, error mapping, policy) and `TestSearchPanel.test.tsx` (component). `documentsApi.test.ts` and `source_guard.test.ts` now cover `search` (final URL loop, read-only classification, no direct `api.search(` outside hooks, no HTML or link from passage text). Existing API mocks gained `search`.
- **Known limits:** the API cannot restrict a search to one document, so the detail-page tab narrows by subject (and optionally class) and results may list other documents of that scope. The submit-handler pending check is a second layer behind the hook's in-flight guard, so removing only that check changes nothing observable.
