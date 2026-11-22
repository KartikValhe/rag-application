# Backend small leftovers after Phase 5f: plan only

Status: **plan, nothing implemented** (2026-11-22). Written from the code as it is in `main` with a clean `git status`.
Source of truth stays `docs/implementation_plan.md`; the API contract is `docs/api/phase5.md`. The four items come from the
"Known leftovers" of `docs/phase5f-plan.md` (items 1 and 3, and the replaced-version workaround in 5f-7).

## Summary

| # | Item | Needs a migration? | Needs a frontend file touched? | Size |
|---|---|---|---|---|
| 1 | `updated_at` on a version | **No, the column already exists.** Only the response model lacks it | No (the Zod schema may add it later) | small |
| 2 | Reach the replaced (archived) version after a reload | No | **Yes**: the drift guard allow-list (see 2.5) | small to medium |
| 3 | `versions_by_status` and live upload counts on the usage route | No | No | medium |
| 4 | `phase5.md` numbering (11.3 / 11.4) | No | No (works unchanged, one optional hardening) | small |

Suggested order: 4, then 1, then 3, then 2. Items 1, 3 and 4 are additive and break no existing field. Item 2 adds a documented
route, and that alone makes the frontend drift guard fail until the frontend knows about it.

**Decisions I need from the owner** (details in each item):

- Item 3: is "per user" the *caller's own* count (my assumption, matches the 5f-9 meter), or a list of every user's count?
- Item 2: may one line be added to `ROUTES_NOT_USED_BY_FRONTEND` in `frontend/.../apiDriftCheck.ts` in the same step? CLAUDE.md says
  frontend edits are for Phase 5f only, so I will not touch it without a yes.

---

## Item 1: `updated_at` on document versions

### 1.1 Current state

- **The column exists.** `app.document_versions.updated_at TIMESTAMPTZ NOT NULL DEFAULT now()` is in migration
  `backend/alembic/versions/0008_phase5_documents.py:201`; the model has it at `backend/app/db/models/document.py:360-362`
  (`server_default=func.now(), onupdate=func.now()`). It is not nullable, so there is nothing to backfill.
- **The API does not return it for a version.** `DocumentVersionResponse` (`backend/app/schemas/document.py:29-52`) has `created_at` and
  `published_at` but no `updated_at`; `_version_response()` (`backend/app/api/v1/documents.py:143-162`) therefore cannot send it.
  Only the polling model `DocumentStatusResponse` has it (`schemas/document.py:79-89`, built at `documents.py:744-753`). So the
  frontend could read it only by calling the status route once per version.
- **Every writer already sets it explicitly** (no trigger exists). I checked each write to `document_versions` in `backend/app`:

  | Transition | Where | Sets `updated_at` |
  |---|---|---|
  | upload / replace (insert) | `_commit_version`, `documents.py:339` | column default `now()` |
  | worker claims the job (`processing`, stage `parsing`) | `workers/ingest.py:226-231` | yes |
  | worker enters a stage | `workers/ingest.py:260-268` | yes |
  | worker fails or re-queues a version | `workers/ingest.py:239-257` | yes |
  | index transaction (`ready`, counts, flags) | `workers/pipeline.py:246` | yes |
  | sweeper marks a stuck version `failed` | `workers/sweeper.py:204` | yes |
  | retry route (`queued` again) | `documents.py:787-791` | yes |
  | publish: previous version `archived` | `services/document_lifecycle.py:90` | yes |
  | publish: new version `published` | `services/document_lifecycle.py:122-126` | yes |
  | soft delete archives versions | `documents.py:699-707` | yes |
  | `rescan_flags` script | `scripts/rescan_flags.py:143` | yes |

  Version delete and prune remove the row, so there is nothing to stamp.
- **Something else depends on it.** The sweeper uses `v.updated_at` as the age of a `queued` version that has no job
  (`workers/sweeper.py:88-93`). A trigger or any "touch on every change" would change that behaviour.

### 1.2 Proposed change

**No migration, no trigger.** The question's migration (name, type, default, backfill) is not needed: the column is there with the right
type and default. The work is the contract:

1. Add `updated_at: datetime` to `DocumentVersionResponse` and set it in `_version_response()`. One field, always present
   (the models keep "every key is always present"). It flows into the list, detail, upload, retry and publish responses because they all use
   `_version_response`.
2. `docs/api/phase5.md` section 5, `DocumentVersionResponse`: add `updated_at` and define it in one sentence: *"the last time any column of this
   version changed (status, stage, error, counts, flags); transaction time. Use `published_at` for the time of publishing."*
3. Frontend (later, not in this step): add `updated_at` to `DocumentVersionSchema` and show "last changed".

Why no trigger: the repo's pattern is an explicit `updated_at = now()` in every statement (documents and `ingest_jobs` do the same); a trigger would be a
new mechanism, a migration on three roles' tables, and it would silently bump the sweeper's age signal. The guard against a future forgotten write is
a test (1.4, test 4), not a trigger. If the owner still prefers a database guarantee, the migration would be `0012_document_versions_touch.py` with a
`BEFORE UPDATE` trigger on `app.document_versions` (SECURITY INVOKER, so RLS still applies); I do not recommend it.

Meaning to tell the frontend: it moves on **stage changes** during processing and on a flag rescan too, not only on status changes. `now()` is the
transaction start time.

### 1.3 Security and tenant isolation

None. A timestamp of a row the caller can already read, on routes that already apply scope and RLS. No new query, role or grant.

### 1.4 Tests first

| # | Test (file) | Fails today because |
|---|---|---|
| 1 | `DocumentVersionResponse` has an `updated_at` field (fast, `tests/test_document_version_updated_at.py`) | the field does not exist |
| 2 | Upload, list, detail, retry and publish responses carry `updated_at` and it equals the database value (db) | key missing; also the exact key set in `test_document_upload.py:39,72` must be extended on purpose |
| 3 | `updated_at` strictly increases on every transition: backdate the row with `raw_db`, then drive queue, claim, stage, ready, retry, publish (new and archived) and rescan, and compare (db) | passes today: it is a **regression** test. Prove-it in the test DB: drop `updated_at = now()` from `mark_published` and test 3 must fail |
| 4 | Source guard: every `UPDATE app.document_versions` string in `backend/app` (found with `ast`, adjacent string literals are one constant) and every `update(DocumentVersion)` mentions `updated_at` (fast) | passes today; fails the day someone adds a writer without it |

Tier map: add the new file to `tests/tier_map.py` (it fails collection otherwise). Existing tests to extend deliberately:
`test_document_upload.py:39-72` (`VERSION_KEYS`).

### 1.5 Risks

- Contract tests with an exact key set fail until updated (expected, listed above).
- A frontend that shows "last changed" will see it jitter during processing (stage changes). Documented in 1.2.
- The Zod object ignores unknown keys, so the running frontend is not affected before it adopts the field.

### 1.6 Estimate

**Small** (about half a day including docs).

---

## Item 2: reaching the replaced version after a reload

### 2.1 Current state: **not reachable**

- `DocumentResponse` (`schemas/document.py:55-71`) has only `latest_version` and `published_version`. `_build_documents()` loads **all** versions of each document
  (`documents.py:184-190`) but keeps only the highest `version_no` and the one `published` (`documents.py:191-196`).
- After publishing v3, v3 is both latest and published; v2 is `archived` and appears nowhere. There is no list-versions route. The version routes in
  `phase5.md` section 4.1 all need a `version_id` the page would have to remember: status, retry, publish, delete, download, flags.
- The frontend knows this and keeps a workaround in memory that a reload loses: `frontend/src/features/documents/pages/MaterialDetailPage.tsx:103-105`
  and `docs/phase5f-plan.md:599`.
- The archived row stays (the newest `DOC_MAX_ARCHIVED_VERSIONS`, default 5, are kept: `document_lifecycle.py:137-158`) and its file is still
  downloadable until the outbox deletes it after `DOC_ARCHIVE_FILE_RETENTION_DAYS` (30): `document_lifecycle.py:98-111`, `documents.py:910-952`.
- Same gap for an older **failed** version once a newer upload exists, and a document whose only version was deleted shows `latest_version: null`
  (5f leftover 2).

### 2.2 Proposed change (smallest)

One new read route, nothing else changes:

`GET /api/v1/documents/{document_id}/versions` (permission `document:view`)

- Response `DocumentVersionListResponse`: `{items: DocumentVersionResponse[], has_more: boolean}`, newest `version_no` first, `LIMIT 100`.
  The limit is needed because failed versions are never pruned (only archived ones are: `prune_archived`), so a list can in theory grow.
  `has_more` tells the UI it was cut. No cursor: 100 is far above the normal count (about 5 archived + 1 published + 1 draft + a few failed).
- Scope: the same call as `GET /{document_id}` (`document_scope.load_document_for(db, user, document_id, action="view")`). Unknown id, another
  tenant's id, out-of-scope document and soft-deleted document all give the one generic `404 not_found`. Students get `403 permission_denied`.
- Items reuse `_version_response()`, so they also carry `retryable`, and (after item 1) `updated_at`. No file path, storage key or sha256 is added.
- Declared as a `GET` on the same path as the existing `POST .../versions`; it does not collide with `/limits` or `/test-search`.
- Docs: one row in `phase5.md` section 4.1 and one model in section 5.

Rejected alternatives: putting `archived_versions` into `DocumentResponse` (it grows every list response, up to 100 documents per page, and changes the most
used model); a single `previous_version` field (does not solve "the list").

### 2.3 Security and tenant isolation

- No new table or grant. Runs on `TenantDB` (role `app_tenant`, NOBYPASSRLS, `app.tenant_id` set per transaction); policy `tenant_isolation` on
  `app.document_versions` and `app.documents`. The query also filters `tenant_id` and `document_id` explicitly, like `_load_version`.
- The route must not become a probe: the scope check runs first and a version of another document can never appear because the query is by
  `document_id` after the scope check.
- It exposes archived versions to the same people who can already download them by id (`document:view` + one linked pair in scope). No new reader.
  Archived versions have no chunks (deleted at publish), so test search is unaffected.

### 2.4 Tests first

| # | Test | Fails today because |
|---|---|---|
| 1 | After replace + publish the list returns v2 `archived` and v3 `published`, newest first, same keys as `DocumentVersionResponse` (db) | route is a 405 (the path only has POST) |
| 2 | Generic 404 for unknown id, another tenant's document, a teacher outside the pair, and a soft-deleted document, with the same body as every other document route (db, security) | 405 instead of 404 |
| 3 | A student and a role without `document:view` get 403 (db, security) | 405 |
| 4 | Another tenant's versions never appear, also when the explicit `tenant_id` filter is removed in a prove-it run in the test DB (RLS alone holds) (db, security; add to the isolation coverage) | n/a until the route exists |
| 5 | More than 100 versions: `items` has 100, `has_more` is true (db) | route missing |
| 6 | OpenAPI check: the new `GET` is not in the upload registry and the existing registry test still passes (`tests/test_upload_preauth.py`) | n/a (guards against a wrong registry entry) |

### 2.5 Risks

- **Drift guard.** Documenting the route makes `api_drift.test.ts` fail ("the doc lists GET /documents/{}/versions but the frontend never calls it") and
  also the self-test "the unchanged doc has no drift". Fix is one entry in `ROUTES_NOT_USED_BY_FRONTEND` in `apiDriftCheck.ts`, or the frontend
  starting to call it in the same step. That is a frontend file, hence the question to the owner. Do the doc edit and the guard edit together.
- An archived row can outlive its file (see 3 under "Surprising"): the list says `archived`, and a download after retention gives a 404. The route does not
  try to say whether the file still exists; the frontend must treat a 404 on download as "file no longer kept".
- Body limit: a `GET` under `/api/v1/documents` falls under the 64 KB rule; no registry entry is needed (verified by test 6).

### 2.6 Estimate

**Small** for the route, tests and docs; **medium** if the frontend step is counted.

---

## Item 3: usage route, `versions_by_status` and live upload counts

### 3.1 Current state

- `GET /api/v1/admin/documents/usage` (`documents.py:1180-1191`, permission `settings:manage`) returns `DocumentUsageResponse`
  `{documents, storage_bytes, pages, limits}` (`schemas/document.py:116-122`). The usage numbers come from `document_upload.tenant_usage()`
  (`services/document_upload.py:225-247`), which **the upload path also uses for the quota check** (`check_quota`, line 250), so I will not change it.
- The hourly upload limits are **Redis sliding windows**, not database counts: `check_upload_rate()` (`document_upload.py:114-137`) takes one slot
  per upload in `ratelimit:doc_upload:user:{user_id}` and `ratelimit:doc_upload:tenant:{tenant_id}` (prefixes at lines 42-43, window 3600 s).
  `SlotManager.count(key)` already exists (`core/slots.py:261-270`).
- The slot is taken **before** the file is read and validated (`documents.py:476-478`, `495-497`) and is never released, so an upload that is rejected
  for its format or size still uses one slot.
- Existing test `test_document_routes.py:426` asserts the exact body keys `{"documents", "storage_bytes", "pages", "limits"}`.
- The frontend schema is lenient and strips unknown keys (`documentSchemas.ts:129-155`), and the drift guard only checks that frontend keys exist in
  the doc, so additive fields break neither.

### 3.2 Proposed change (additive; existing fields untouched)

```json
{
  "documents": 12, "storage_bytes": 3400000, "pages": 210, "limits": { "...": "unchanged" },
  "versions_by_status": { "queued": 0, "processing": 1, "ready": 2, "published": 12, "archived": 7, "failed": 1 },
  "uploads_last_hour": { "tenant": 14, "caller": 3, "window_seconds": 3600 }
}
```

- **`versions_by_status`**: always all six statuses (`DOCUMENT_VERSION_STATUSES`), zero when none. Counts versions of **live** documents only, the
  same rule as the other usage numbers ("deleted materials are not counted"; a soft delete archives every version, so counting them would inflate
  `archived`). New function `document_upload.version_status_counts(db, tenant_id)`:

  ```sql
  SELECT v.status, count(*) FROM app.document_versions v
  JOIN app.documents d ON d.tenant_id = v.tenant_id AND d.id = v.document_id
  WHERE v.tenant_id = :t AND d.deleted_at IS NULL GROUP BY v.status
  ```
- **`uploads_last_hour`**: read from the **same Redis windows the limiter enforces**, via `slots.default_manager.count(...)`, so "limit minus count" is
  the real remaining quota. `caller` is the calling admin's own count against `uploads_per_user_per_hour`; `tenant` is the tenant window against
  `uploads_per_tenant_per_hour`. The window is **rolling 60 minutes**, not the clock hour (field `window_seconds` says so).
  - It counts upload *attempts that passed the scope check*, so it can be higher than the number of versions created (see 3.1). The doc must say it.
  - If Redis is unreachable `count()` raises a 503 outside development (`slots.py:267-269`). For a read-only display that is wrong: catch it and return
    `null` for both numbers (and log a warning). `documents`, `limits` and `versions_by_status` still render. The limiter itself stays fail-closed.
  - **No per-user list.** Redis offers no cheap way to list other users' keys (a `SCAN` is not acceptable), and I do not want to expose other users'
    ids. If the owner wants a breakdown per user, use the alternative below.
- Docs: `phase5.md` section 5 `DocumentUsageResponse` line (no backticks inside the braces: the drift guard's model regex).

**Alternative if a per-user breakdown is wanted (not recommended as the default):** count from the database,
`created_at >= now() - interval '1 hour'` grouped by `uploaded_by`. It counts *accepted* uploads only (so it would disagree with the limiter), and the
index `ix_document_versions_tenant_created (tenant_id, created_at)` becomes a small migration `0012`. At today's size (a tenant has a few hundred to a few
thousand version rows, ceiling 10,000 documents) a scan of the tenant's rows is cheap even without it. I would only do this on request.

### 3.3 Tenant scope and row-level security

- **Database part** (`versions_by_status`): the route depends on `TenantDB` (`api/deps.py:207`, `get_tenant_session`). That opens a transaction as role
  **`app_tenant`** (NOBYPASSRLS) and runs `set_config('app.tenant_id', ..., true)`. Both tables have ENABLE + FORCE RLS and the policy
  **`tenant_isolation`** (`tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`, created by `_secure_table` in
  `0008_phase5_documents.py:101-110`). `app_tenant` already has SELECT on both, so **no migration and no grant**. The query also filters
  `v.tenant_id = :t` explicitly (repo convention); a call with no tenant context returns zero rows.
- **Redis part** (`uploads_last_hour`): Redis has no RLS. Isolation is by **key construction**: both keys are built from the authenticated `user`
  (`user.tenant_id`, `user.id`), never from a query parameter or body. That is the property to test.
- Who may read: `settings:manage` only (tenant admin). The caller sees their own count and the tenant total, nobody else's.

### 3.4 Cost

- `versions_by_status`: one extra grouped query, reading only the tenant's rows by the primary-key prefix `(tenant_id, id)`, joining `documents` by its
  primary key. No new index. Same order of cost as the existing `tenant_usage` totals query. The usage page is not polled.
- `uploads_last_hour`: two Redis `EVAL`s of 2 commands (`ZREMRANGEBYSCORE` + `ZCARD`) with a 1 s timeout. Note `count()` also trims expired entries, a
  write, which `acquire` does too.

### 3.5 Tests first

| # | Test | Fails today because |
|---|---|---|
| 1 | Versions in every status, plus an archived version of a soft-deleted document: body has all six keys with the right counts and the deleted one is not counted (db) | key missing |
| 2 | Tenant B's versions never change tenant A's counts (db, security). Prove-it in the test DB: remove the explicit `tenant_id` filter from the query and the test must still pass (RLS holds); `version_status_counts` run on a session with no tenant context returns zeros | key missing |
| 3 | Uploads as teacher 1 (x2), teacher 2 (x1) and in another tenant: admin sees `tenant == 3`, `caller == 0`; the same admin after uploading once sees `caller == 1`; the other tenant's admin sees only its own (db, security) | key missing |
| 4 | An upload rejected for its format still counts (documents the "attempts" meaning) (db) | key missing |
| 5 | Redis down (patch the manager to raise): 200 with `uploads_last_hour` values `null`, `documents`/`limits`/`versions_by_status` intact (db) | key missing |
| 6 | Fast unit test with a fake manager: the two keys are built from `user.tenant_id` and `user.id` only (fast, security) | helper missing |
| 7 | Update the exact-key assertion at `test_document_routes.py:426` to include the two new keys (deliberate contract change, not a weakening) | will fail once the fields exist |

### 3.6 Risks

- The "attempts, not uploads" meaning can surprise an admin; the doc and the UI wording must say "upload attempts".
- Rolling window vs clock hour: wrong if the UI says "this hour" literally.
- If the new function ever shared code with `tenant_usage`, the quota check could change; I keep them separate on purpose.
- `null` counts when Redis is down must not be shown as 0 by the frontend (its `lenientCount` already turns null into "Not available").

### 3.7 Estimate

**Medium** (one service function, one Redis helper with the error handling, schema, doc, about seven tests).

---

## Item 4: `phase5.md` numbering

### 4.1 Current state

`docs/api/phase5.md` section 11:

- `### 11.3 Injection codes` (line 316) holds the flag-code table (line 326), several paragraphs, **and then a second table, the test-search HTTP codes
  (lines 347-352)**, which has nothing to do with injection codes.
- `### 11.4 Validation errors on every route (5d-2b)` (line 354) has no table; it is about FastAPI's generic 422.
- Cross references: line 287 says "section 11.4" for the 422 list; line 308 says "section 11.3" for `rescan_flags` (still true).

### 4.2 Proposed doc fix (exact)

1. Insert, directly before the line `| Code | HTTP | Where |` at line 347 (after the "Limits" bullets of 11.3), this heading and sentence:

   ```markdown
   ### 11.4 Test-search error codes

   Codes of `POST /api/v1/documents/test-search` (11.1). Every message is fixed text.
   ```
2. Rename line 354 `### 11.4 Validation errors on every route (5d-2b)` to `### 11.5 Validation errors on every route (5d-2b)`.
3. Line 287: change "(FastAPI's `422` list, section 11.4)" to "section 11.5".
4. Result: 11.3 has exactly one table (the injection codes), 11.4 is the test-search error table, 11.5 is validation errors.
5. `docs/phase5d-plan.md:333` says "sections 10, 11.1-11.4"; change to "11.1-11.5" (history pointer, optional). `docs/phase5f-plan.md:100`
   ("section 11.3", the tripwire wording) stays correct.
6. Optional, same edit: the intro sentence at `phase5.md:3` is stale ("Publish, flags and test search come in 5c/5d; until a worker exists (5b-2) uploaded
   versions stay `queued`"). Both are built.

**I checked this against the real guard** without touching the repo: I built a copy of the doc with this exact change in the scratchpad and ran
`api_drift.test.ts` and `api_drift_selftest.test.ts` on it through the guard's own `PHASE5_API_DOC` override.

| Version of the doc | Guard tests | HTTP error codes the guard reads |
|---|---|---|
| today | 15 of 15 pass | 49 |
| **this fix** (new 11.4 heading, validation errors become 11.5) | **15 of 15 pass** | **49 (none lost)** |
| alternative: move the table into 11.1, keep 11.4 | 15 of 15 pass | **43: silently loses** `invalid_query`, `query_too_long`, `test_search_rate_limit_exceeded`, `search_unavailable`, `search_timeout`, `search_busy` |

So the alternative also "passes" while the guard quietly stops checking six codes. That is why I recommend the first one.

### 4.3 What the drift guard needs from the doc (so this edit and later ones keep it working)

The guard (`frontend/src/features/documents/__tests__/apiDriftCheck.ts`) reads the doc as text. It needs:

- **Headings** (`^#{1,6} `, matched by their number prefix) to exist: `## 2.`, `## 5.`, `## 7.`, `## 8.`, `## 9.`, `### 4.1`, `### 11.1`, `### 11.3`,
  `### 11.4`. A missing one throws "the doc has no section matching ...". A section ends at the next heading of the same or a higher level.
- **Tables** as rows starting with `|`. Error codes come from the **first column**, backticked, `[a-z][a-z0-9_]*`, in sections 2, 7, the second table of 11.3
  (today) **and** the first column of the `11.4` section. With the fix, "the second table of 11.3" is empty and the `11.4` lookup finds the test-search
  table, so the union is unchanged. This works by the union, not by design (see 4.5).
- `11.3`'s **first table** is the injection flag codes (at least 8).
- Section 4.1 route rows: first cell `` `METHOD /path` `` (a path starting with `...` is expanded to `/api/v1/documents/{document_id}`), second cell
  backticked permissions `a:b`, third cell the request field names (upload row: `` `title` (1-200 characters ``, `` `class_ids` (...1-50) ``; list row;
  flags row). At least 15 routes.
- Section 5 models in the form `` `Name`: `{...}` `` or `` `Name` (note): `{...}` ``, with **no backtick inside the braces**; at least 8 models; each
  frontend schema key must be listed for its model.
- Sections 8 and 9: first-column codes, plus the line starting "Unchanged for comparison" (first sentence). At least 12 failure codes.
- Numbers: the `TestSearchRequest` line `k?: integer 1-10`, and `` `RETRIEVAL_MAX_QUERY_CHARS` (500) `` in 11.1.
- **Exact strings used by the self-test** (`api_drift_selftest.test.ts`): `| `duplicate_version` | 409 |`, `| `invalid_title` | 422 |`,
  `| `subject_has_documents` | 409 |`, `| `scanned_pdf` |`, `| `role_play` |`, `| `POST /api/v1/documents` | `document:upload` |`,
  `` `POST /api/v1/documents/test-search` ``, `| `GET .../versions/{version_id}/status` |`, `flagged_chunk_count: integer | null, flags_acknowledged`,
  `` `DocumentLimits`: `{max_file_bytes, ``, `` `TestSearchRequest` (5d-2): `{query: string, ``, `` `RETRIEVAL_MAX_QUERY_CHARS` (500) ``.
- CRLF is fine (`\r?\n`).
- Other readers of the doc: three backend tests only check that code names appear (`test_upload_profiles.py:361`, `test_pdf_embedded_file_rule.py:88`,
  `test_worker_review_fixes.py:385`); renumbering does not affect them.
- **New doc rows for items 1 to 3:** the `DocumentUsageResponse` and `DocumentVersionResponse` fields are safe. The new route row of item 2 makes the guard
  fail until the frontend allow-list is updated (2.5).

### 4.4 Security and tenant isolation

None (documentation only).

### 4.5 Tests first

| # | Test | Fails today because |
|---|---|---|
| 1 | New fast backend test `tests/test_phase5_doc_layout.py`: `### 11.n` headings are consecutive; the section holding the `invalid_query` row has "error codes" in its title; `### 11.3` contains exactly one table | 11.3 has two tables and the test-search table sits under "Injection codes" |
| 2 | The guard's own tests on the edited doc (`api_drift.test.ts`, `api_drift_selftest.test.ts`) | pass before and after; this is the "nothing broke" check, already verified on the scratch copy |

Optional frontend hardening, **not** part of this backend step: make the guard find sections by title instead of number, read the test-search table from its
own heading, update the messages that still say "11.3 / 11.4" (`apiDriftCheck.ts:182` and `:341`), and raise the "fewer than 30 codes" floor to about 45 so that
losing six codes fails (the table above shows 49 today).

### 4.6 Risks

- External links to the old anchors of 11.4 would now land on the test-search table (I found none in the repo's markdown).
- Forgetting step 3 leaves one wrong cross reference.

### 4.7 Estimate

**Small** (under an hour, plus the one test).

---

## Wrong or surprising things I found

1. **Item 1 needs no migration.** The column exists, is NOT NULL, and every writer sets it. The 5f record ("the version response has no such field") is right; the
   gap is only the response model.
2. **The sweeper depends on `updated_at`** (`workers/sweeper.py:88-93`): a trigger or touch-on-read would change when a queued version without a job is recreated.
3. **An archived row can outlive its file.** At publish the old file is queued for deletion after 30 days (`document_lifecycle.py:98-111`), but the row stays
   (up to 5 are kept). After that the row says `archived` and a download returns the generic 404. Nothing in the API tells the UI.
4. **Failed versions are never pruned** (`prune_archived` only touches `archived`), so a document's version list is unbounded; an older failed version also
   becomes unreachable once a newer upload exists. A document whose only version was deleted shows `latest_version: null`.
5. **The upload rate slot is spent before validation and never returned** (`documents.py:476-478`, `495-497`). A rejected upload (wrong format, too large) uses
   one of the hourly uploads. It is documented in section 4.2 of the API doc but will look odd next to a live counter.
6. **`slots.count()` fails closed (503) outside development** (`core/slots.py:267-269`). Right for the limiter, wrong for a read-only meter, so item 3 catches it.
7. **The drift guard has a weak floor.** It only fails below 30 error codes while the doc has 49, so six codes can vanish and it still passes (shown by the
   scratch run above).
8. **Documenting a new route breaks the drift guard by design** until the frontend allow-list changes; that is a frontend-file change in a backend step.
9. **Stale text in `phase5.md`**: line 3 still says publish, flags and test search "come in 5c/5d" and that versions stay `queued` until a worker exists.
10. **CRLF working tree.** `docs/api/phase5.md` is LF in git but CRLF on this Windows checkout (`attr/text eol=lf`), so a diff of the edit must be read with that in mind.

## Out of scope on purpose

No change to `tenant_usage`, quotas, the limiter, retry, publish or the worker. No student route. No frontend file is changed by this plan; the two
places where the frontend must follow are named above (2.5 and 4.5). Nothing was committed or pushed.
