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
| B14 | `app_tenant` has table-wide `SELECT` on `app.tenants` (including `db_dsn`); narrow to a column grant | Phase 5 design 0d | Separate review |
