# Backlog

Items deferred or noticed during Phase 4 and the Phase 5/6 design review (2026-11-15). No earlier backlog file existed, so this list is derived from `docs/implementation_plan.md`, `docs/api/phase4.md` and a read of the code. "Unverified" means not re-checked against the running app.

## Phase 4 (timetables) follow-ups

| # | Item | Source | Target |
|---|---|---|---|
| B1 | One-day overrides: holidays, cancelled or changed lectures (today "next lecture" ignores them) | Plan, Phase 4 risks | Phase 10 |
| B2 | Substitutions and multiple terms | Plan, Phase 4 risks | Phase 10 |
| B3 | LLM-assisted extraction for scanned or irregular timetables (once per upload, behind a flag and quota, still ends in the review screen) | Plan, Phase 4 | Phase 10 |
| B4 | PPTX passes upload validation but is not importable as a timetable (`unsupported_format`) | `docs/api/phase4.md` 5.3 | Optional |
| B5 | `.ics` export per class (listed as optional in the plan; not in `phase4.md`, so probably not built: unverified) | Plan, Phase 4 | Optional |
| B6 | `schedule.class_schedule()` uses `date.today()` (server time zone) for item dates instead of the tenant time zone | `app/services/schedule.py` | **DONE 2026-11-15** (step 5a-1): `class_schedule()` now uses the tenant time zone and takes an injectable `now`; test `test_class_schedule_dates_use_tenant_timezone`. The other schedule functions already used the tenant zone |
| B7 | PDF rule relaxed for timetable imports as well (`/OpenAction` and `/AA` allowed; `/RichMedia` and XFA rejected), with tests | Phase 5 design, decision 4 | Step 5a |

## Platform and security follow-ups

| # | Item | Source | Target |
|---|---|---|---|
| B8 | Teacher system role holds `class:manage` and `subject:manage`; review whether teachers should have them | `app/services/rbac.py` | Separate review |
| B9 | `verify_scope(CLASS_SUBJECT)` checks only enrollment for students and ignores the subject; do not reuse it for document or retrieval scope | `app/core/authz.py` | Use the pair-based resolver (design A4) |
| B10 | `TENANT_TABLES` in `tests/test_isolation.py` is hand-maintained although the plan says auto-discovered | `tests/test_isolation.py` | Guard test in 5a |
| B11 | `SlotManager` has no lease renewal, and its 503 message says "Timetable import" | `app/core/slots.py` | Step 5a |
| B12 | `README.md` mentions a `requirements.txt` that does not exist (dependencies are in `pyproject.toml`) | `CLAUDE.md` | Docs fix |
| B13 | Chat-log retention policy (period, deletion job, tenant-admin setting) | Phase 5 design, decision 10 | **Before the pilot** (moved up from Phase 9) |
| B14 | `app_tenant` has table-wide `SELECT` on `app.tenants` (including `db_dsn`); narrow to a column grant | Phase 5 design 0d | **DONE 2026-11-15** (migration `0010_tenants_rls.py`, part 2): RLS enabled and forced on `tenants`, per-role policies, `app_tenant` reads only `id, slug, status, timezone` and updates only `timezone`; `app_platform` cannot change `id`/`slug` or set an explicit id. Tests: `tests/test_tenants_rls.py`, inventory tests in `tests/test_isolation.py` |
| B15 | **Tenant offboarding order.** `storage_deletions.tenant_id` is `ON DELETE RESTRICT` (migration 0009), so deleting a tenant fails while outbox rows exist. Offboarding must first drain the outbox (delete the objects, then the rows) and purge everything under the `t/{tenant_id}/` object prefix, and only then delete the tenant | Security review of 5a-2, finding 4 | Before the first tenant is offboarded; 5b/5c sweeper work |
| B16 | Verify on **Supabase staging** that `REVOKE ALL ... FROM anon, authenticated` really ran for the 7 Phase 5 tables and that the API roles have no grant (including column grants). Those roles do not exist locally, so the migration's `DO` block is untested here | Phase 5 design A2, review of 5a-2 | Before the first staging deploy of 0008/0009 |
| B17 | Slow test fixtures: about 2.7 s per isolation test (each one builds tenants, roles and parent rows and commits). Share a session-scoped pair of tenants or batch the setup | Review of 5a-2 | Before the suite passes about 5 minutes |
| B18 | **Tenant offboarding runbook** (written procedure): stop writes, drain `storage_deletions`, purge `t/{tenant_id}/`, export or delete audit data per policy, then delete the tenant row (cascades the rest). Include the check that no `storage_deletions` rows remain | Review of 5a-2, finding 4 | Before the pilot |
| B19 | `document_classes` rows cascade away when a class-subject link is removed (`fk_document_classes_class_subject` is `ON DELETE CASCADE`), so a document can silently lose a class. Decide whether to restrict it, as was done for subjects, or to warn in the UI | Noticed while fixing finding 3 | 5b (document API) |
| B20 | Frontend error mapper for the new stable code `subject_has_documents` (409 on subject delete); the backend side is done | Review of 5a-2, finding 3 | Frontend task |
| B21 | `platform_read_invitations` policy (migration 0001) lets platform admins read every tenant's invitations; review whether the first-admin flow can use a narrower path | Test-DB safety task | Separate review |
| B22 | Public pre-auth lookups (login, refresh, register, forgot password, public branding, token routes) run on the platform pool and read the whole `tenants` row. Consider a `SECURITY DEFINER` lookup function (by slug, returning only id, slug, status, registration_mode, name) so the platform pool needs no table-wide `SELECT` | Part 2 decision (option ii), `0010` | Separate review |
| B23 | Move the registration-mode routes (`GET`/`PATCH /admin/settings/registration-mode`) from the platform pool to `app_tenant`: needs `GRANT SELECT (registration_mode)` and `UPDATE (registration_mode)` to `app_tenant`, a new migration and an updated column inventory test. It would also make the update and its audit entry one transaction (today they are two sessions with two commits) | Part 2 decision 4 | After B22 |
| B24 | Try migration `0010` (and the `0008`/`0009` grants, see B16) on a **Supabase staging** project before production: RLS enabled and forced, the column grants, `REVOKE ALL` from `anon`/`authenticated`, and that the migration role passes the `rolsuper OR rolbypassrls` check | Part 2 decision (e) | Before the first staging deploy of 0010 |
| B25 | Merge the per-request tenant active/slug lookup in `api/deps.py` into the request's own tenant session. It now opens its own session and transaction, so every authenticated request makes one extra round trip and borrows a second pooled connection | Part 2, step 1 | Performance review |
| B26 | `POST /auth/verify-email`, `POST /auth/reset-password` and `POST /admin/invitations/accept` look up `email_tokens` (and `invitations`) on the platform pool, but `app_platform` has no grant on `email_tokens` (and `tenant_isolation` hides rows without tenant context), so they may fail at runtime. Not caused by 0010; no test covers these routes. Unverified | Noticed while reading `api/v1/auth.py` | Separate review |
