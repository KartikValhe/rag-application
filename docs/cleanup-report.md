# Backend cleanup report (phase 1 inventory + phase 2 proposal)

Date: 2026-11-19. Scope: `backend/`, mostly `backend/tests`. Nothing has been deleted or changed yet. Every
mutation check below was run on the test database only (`ragplatform_test`), one pytest process at a time,
and each one was undone with `git checkout -- <file>`, followed by an empty `git diff --stat -- app`.

## Summary

- **Tests:** 21 test items can be deleted. All 21 are exact copies or strict subsets of another test, and a
  mutation check proved that the other test still catches the rule. Seven pairs of tests can become
  parametrized tests, and one 25-second test can be shortened to about 2 seconds. Nothing on your list of
  security guards loses its last test.
- **Speed:** this cleanup saves about **29 s out of about 1,325 s** (2%). The suite's time is spent somewhere
  else (see "Where the time goes"), so cleanup will not make it fast.
- **App code:** 7 dead items in `app/`, all small (functions, an alias, a class, two re-exports). All of them
  are in modules that `main.py` imports, which rule 3 protects. **I need your decision** before I touch them
  (question Q1).
- **Dependencies:** `orjson` and `jinja2` are declared but never imported.
- **Stray files:** none. No caches, `.bak`, `.orig` or `.log` files and no scratch scripts are tracked. No
  tracked file is empty; the five tiny ones are package `__init__.py` markers.
- **Baseline problem (not caused by this work):** 1 test fails locally (`test_the_worker_entry_point_still_refuses_to_start`).
  See "Baseline".

## Baseline ("before")

| Item | Value |
|---|---|
| Tracked files in `backend/` | 218 (`app/` 92, `tests/` 109, other 17) |
| Test items collected | 2,065 |
| Full run, part 1 (`-m "slow and not security"`) | `267 passed, 6 skipped, 1792 deselected in 677.86s (0:11:17)` |
| Full run, part 2 (`-m "not (slow and not security)"`) | `1 failed, 1775 passed, 12 skipped, 273 deselected, 4 xfailed in 647.21s (0:10:47)` |
| Total time | about 1,325 s (22 min) |
| ruff | `All checks passed!` |
| mypy | `Success: no issues found in 91 source files` |
| Coverage, all of `app/` | 79% (9,828 statements, 2,085 missed) |
| Coverage, guarded areas | `app/core` 92.15%, `app/services` 82.38%, `app/workers` 93.01%, `app/ml` 95.53% |

The coverage run printed the same two summary lines, with times of 684.99 s and 690.04 s. Per-file coverage
and per-file timings are saved outside the repo in the session scratchpad (`coverage_before.txt`,
`coverage_areas_before.txt`, `durations_before.txt`).

**The one failure:** `tests/test_ml_loader.py::test_the_worker_entry_point_still_refuses_to_start` expects
`python -m app.workers.ingest` to exit with code 2 ("not configured"). Since 5c-2 the worker *starts* when the
model, the database, Redis and the store all check out. On this machine they do, so the worker ran until the
60-second timeout. In CI no model is configured, so the test passes there. The worker child inherited the
`_test` database URLs that conftest rewrites, so it touched only `ragplatform_test`. I left the test alone
(it is not in the scope of a cleanup) and propose a backlog line for it.

**Run-time note:** each half of `run_tests full` now takes more than 10 minutes, so the advice in CLAUDE.md
("run it in parts to stay under the 10-minute command limit") no longer holds with two parts. Proposed for the
backlog.

## Table 1: safe to delete

### 1a. Duplicate tests (21 items)

Every row names the test that still covers the rule after the deletion, the mutation applied to `app/`, and
the result of running **only** the remaining test(s) with that mutation in place.

| # | Delete | Items | Remaining coverage | Mutation (in `app/`) | Result with mutation |
|---|---|---|---|---|---|
| D1 | `test_parse_stage.py::test_hidden_docx_text_is_counted_not_extracted` | 1 | `test_document_extract.py::test_docx_hidden_text_is_counted_not_extracted` (in-process, exact blocks) and `security/test_prove_it_fails_parse.py::test_switching_off_hidden_text_skipping_is_caught`. Its "protection ON" step is the same document through the same real stage and sandbox, checked by the same helper. | MC1: `run_is_hidden` always returns False | `2 failed in 1.62s` |
| D2 | `test_ingest_worker.py::test_a_crashing_child_is_a_transient_worker_crash` | 1 | `test_document_parse_child.py::test_crashing_child_is_a_transient_worker_crash` (the crash is mapped to `worker_crash`) and `test_parse_stage.py::test_a_child_that_dies_stays_transient_but_has_a_smaller_attempt_budget[*]` (a real child crash, through the worker, gives RETRY and releases the slots). `test_a_crash_requeues_once_...[child-crash]` covers the worker side as well. | MC2: in `_run_document_child`, `SandboxCrash` raises a permanent error instead | `3 failed, 77 deselected in 5.71s` |
| D3 | `test_timetable_hardening.py::test_timeout_kills_hung_child` | 1 | `test_timetable_import.py::test_killable_parse_timeout_terminates_process`. It runs the same runner, the same `sleep` child, the same 408 / `parse_timeout` / timing / child-is-dead checks, **plus** the release of the slots, so it is a strict superset. (It is *not* a duplicate of `test_sandbox::test_timeout_kills_the_child`, which tests the generic sandbox one layer down.) | MC3a: 408 changed to 400 in the runner. MC3b: `kill_tree` made a no-op | MC3a `1 failed in 2.11s`; MC3b `1 failed in 60.47s` |
| D4 | `test_upload_hardening.py::test_pdf_active_content_rejected` | 8 | `test_upload_profiles.py::test_hostile_pdfs_rejected_in_both_profiles`. It uses the same 8 tokens (`FORBIDDEN_PDF_TOKENS`) and the same code, for **both** profiles. | MC4: `pdf_active_content` never raised | `1 failed in 0.15s` (`timetable: hostile PDF b'/JavaScript' was accepted`) |
| D5 | `test_upload_hardening.py::test_pdf_embedded_file_rejected_with_its_own_code` | 2 | The same test as D4 (the same 2 tokens and code, both profiles). `test_pdf_embedded_file_rule.py` also covers it. | MC5: `pdf_embedded_file` never raised | `1 failed in 0.15s` (`timetable: ... b'/EmbeddedFile' was accepted`) |
| D6 | `test_upload_hardening.py::test_pdf_open_action_and_aa_are_allowed` | 3 | `test_upload_profiles.py::test_open_action_and_aa_allowed_in_both_profiles`: 4 tokens (all 3 names plus `/A#41`) × 2 profiles. | MC6: `OpenAction` and `AA` added to the forbidden names | `8 failed, 41 deselected in 0.26s` |
| D7 | `test_upload_hardening.py::test_pdf_similar_names_not_flagged` | 1 | `test_upload_profiles.py::test_pdf_names_that_only_look_similar_pass`: the same names plus `/Launcher`, both profiles. | MC7: name-end boundary removed (so `/JSON` matches `/JS`) | `2 failed, 47 deselected in 0.19s` |
| D8 | `test_parse_stage.py::test_a_hostile_attribute_is_a_permanent_parse_failed_through_the_real_sandbox[*]` | 2 | `test_review_5b3_child.py::test_a_malformed_outline_level_is_a_permanent_parse_failed` (7 values) and `test_parse_stage.py::test_a_hostile_file_fails_after_one_attempt_through_the_real_worker` (the same `_outline_docx("²")` through the real sandbox **and** the worker). | MC8: a bad outline level is ignored instead of raising `parse_failed` | 7 failed (in-process); `1 failed in 3.62s` (worker) |
| D9 | `test_schedule.py::test_draft_never_visible_to_students` | 1 | It **cannot fail**: its mock returns no entries, so it is the same scenario as `test_no_published_timetable`, with identical asserts. The rule it names is really guarded by `test_timetable_endpoints.py::test_student_denied_on_draft_and_manage_endpoints`. | MC9: schedule query lets draft rows through | Endpoints test failed (`1 failed, 43 passed in 136.30s`); D9 itself **passed** under the mutation |
| D10 | `test_timetable_real_child.py::test_describe_exit_reports_signal_name` | 1 | `test_sandbox.py::test_describe_exit_reads_only_a_strict_marker`: the same function (`tpr._describe_exit` is an alias of `sandbox.describe_exit`) and the same 3 assertions. | MC12: `describe_exit` never reports the signal name | `1 failed in 0.30s` |

Tier note: D3 and D4–D7 leave the gate tier with the same coverage. `test_upload_profiles.py` is `fast`, and
`test_timetable_hardening.py::test_timeout_kills_whole_process_tree` plus the `test_sandbox.py` timeout tests
still prove in the gate that a timeout kills the child. One thing moves to the full tier only: the
**408 status code** of a timetable timeout. Its remaining test, in `test_timetable_import.py`, is `db`, not
`security`.

### 1b. Unused test helpers (no reference anywhere except the definition)

| File:line | Name | Note |
|---|---|---|
| `tests/fixtures/ml.py:39` | `PAD_ID` | constant |
| `tests/fixtures/parse_files.py:243` | `page_texts_of()` | |
| `tests/fixtures/worker.py:262` | `raise_once()` | |
| `tests/test_auth.py:27` | `_login()` | |
| `tests/test_chunker.py:34` | `embedded_tokens()` | |
| `tests/test_document_route_matrix.py:170` | `_UNUSED` | It only keeps the `Callable`/`Awaitable` imports alive; remove those imports too |
| `tests/test_tenants_rls.py:52` | `_set_tz()` | |
| `tests/test_upload_review_followups.py:66` | `MAIN_PART` | constant |

Proof: an AST scan of every top-level name in `tests/`, with a word-boundary search across `tests/` and
`app/`. Each name occurs exactly once, at its definition. Fixtures in `conftest.py` and `tests/fixtures/`, and
every `tests/security/*_assertions.py` helper, are all used. (Four helpers looked unused at first, but they are
called from non-test functions inside the prove-it files.)

### 1c. Unused dependencies (`pyproject.toml`)

| Dependency | Proof |
|---|---|
| `orjson>=3.10.0` | No `import orjson` and no `ORJSONResponse` anywhere in `app/`, `tests/` or `alembic/`. |
| `jinja2>=3.1.4` | No import anywhere. `app/services/email.py` says that "Phase 3 will upgrade to Jinja2", so it was a planned dependency. Remove it now and add it back when it is used? (Q3) |

The others checked out as used: `uvicorn` (Dockerfile `CMD`), `tzdata` (`zoneinfo` on Windows),
`email-validator`, `python-multipart`, `lxml`, `pypdfium2` (imported or required at runtime). There is no
lock file. CI runs `pip install ".[dev]"`, so I would remove the two lines and then check that this install,
ruff, mypy and the tests still pass.

### 1d. Dead code in `app/` (waiting for Q1)

Vulture at 80% confidence found 6 items, all **false positives**. They are required parameters of overridden
interface methods: `qname` in the SAX `ContentHandler` callbacks (`upload_security.py:462,471`) and
`kw`/`dialect`/`coltype` in the SQLAlchemy `UserDefinedType` hooks (`db/models/document.py:136-147`). Vulture
at 60% confidence, followed by a search of every name across `app/`, `tests/`, `alembic/`, `infra/`,
`.github/`, `docs/` and `README.md`, found these real dead items:

| File:line | Item | Status | Proof |
|---|---|---|---|
| `app/core/context.py:41` | `get_request_id()` | DEAD | Only the definition. `set_request_context` still writes the context variable for structlog. |
| `app/core/context.py:60` | `get_request_tenant_slug()` | DEAD | Only the definition. |
| `app/schemas/timetable.py:15` | `class DayOfWeek(int)` | DEAD | Only the definition; the schemas use `int` with `ge=0, le=6`. |
| `app/services/auth.py:111` | `_check_and_increment_throttle()` | DEAD | "Deprecated alias for backwards compatibility". Only the definition. |
| `app/services/invitation.py:167` | `ImportRow.to_dict()` | DEAD | `api/v1/admin.py` builds the response from the attributes itself. |
| `app/services/timetable_parser_runner.py:30` | `_kill_tree` re-export | DEAD | `# noqa: F401 (kept for callers and tests)`, but nothing uses it. |
| `app/services/timetable_parser_runner.py:29` | `_describe_exit` re-export | DEAD after D10 | Its only user is the test deleted in D10. |

No **file** in `app/` is unused. Every module that no other app module imports is an entry point
(`main.py`, the 8 `app/scripts/*` CLIs, `services/document_parse_child.py` and
`services/timetable_parse_child.py`, which are run with `python -m`) or is imported by `alembic/`
(`ml/model_keys.py`).

## Table 2: merge or shorten

These changes do not delete any test. Each test keeps every assertion it has now.

| # | Tests | Change | Result |
|---|---|---|---|
| S1 | `test_security_review.py::test_per_ip_throttle_does_not_block_successful_logins` (25.3 s) | Monkeypatch `settings.LOGIN_THROTTLE_IP_MAX_ATTEMPTS` to 3 (it is read at call time, `auth.py:66`) and do 4 successful logins instead of 60. | Same guard, about 2 s. It is the **only** guard of the rule (see K3). In phase 3 I re-run MC10 against the shortened test to prove it still fails. |
| M1 | `test_branding.py::test_wcag_rejects_bright_yellow_on_white_surface`, `..._dark_color_on_dark_surface`, `..._mid_tone_insufficient_text` | One `@pytest.mark.parametrize("color, fragment", ...)` test. | 3 items, 1 function |
| M2 | `test_security_review.py::test_jwt_secret_fails_if_weak`, `..._if_default_placeholder` | One parametrized test over (keys, expected message). | 2 items, 1 function |
| M3 | `test_schedule.py::test_weekend_query`, `test_no_more_lectures_today` | One parametrized test over `now` (Saturday 14:00, Monday 11:00); both expect next Monday. | 2 items, 1 function |
| M4 | `test_upload_hardening.py::test_vba_project_anywhere_rejected`, `test_embeddings_rejected` | One parametrized test over (part, code). | 7 items, 1 function |

## Table 3: keep (looked suspicious, but each one is needed)

| # | Test(s) | Why it stays |
|---|---|---|
| K1 | The three outage variants in `test_ingest_worker.py` vs. the outage tests in `test_worker_review_fixes.py` | They are complementary. `test_ingest_worker` covers an outage **during a stage** (the attempt counts, exponential backoff, the full budget of 3, three different codes). `test_worker_review_fixes` covers an outage **when the job starts** (the attempt is refunded, flat backoff). These are different code paths. |
| K2 | The fail-closed tests in `test_document_retry_limits` and `test_document_upload` | They guard different callers. MC11 made only `check_retry_rate` fail open: the upload-side test passed (`1 passed in 0.14s`) and the retry-side test failed (`1 failed in 0.25s`). |
| K3 | `test_security_review.py::test_per_ip_throttle_does_not_block_successful_logins` | It is the only guard. MC10 counted successful logins against the IP: `test_throttle.py` passed (`3 passed in 5.76s`; it never sends `X-Forwarded-For`) and this test failed (`1 failed in 22.23s`). It gets shortened instead (S1). |
| K4 | The route-role matrices in `test_authz_rbac`, `test_document_route_matrix` and `test_timetable_endpoints` | They cover disjoint route sets (`/admin/users`; the 13 document routes, built from the route table; the timetable routes). The default-deny check in `test_authz_rbac` covers every route. |
| K5 | `test_document_extract::test_docx_hidden_text_is_counted_not_extracted`, `test_document_parse_child::test_crashing_child_is_a_transient_worker_crash` | These are the remaining guards for D1 and D2 (they fast-fail in MC1 and MC2). |
| K6 | `test_parse_stage::test_timeout_is_permanent_and_crash_is_transient` | It overlaps with `test_document_parse_child` (the same shared mapping in `_run_document_child`), but it is the only test that hangs a child behind `parse_in_sandbox`. It costs about 2 s. |
| K7 | Prove-it "baseline" tests that call a helper exactly like a regular test, for example `test_prove_it_fails_uploads::test_baseline_hostile_pdf_helper_passes` vs `test_upload_profiles::test_hostile_pdfs_rejected_in_both_profiles` (also the store, zip-name, slide, registry, duplicate-header and PDF-scan helpers) | Rule 5 protects the prove-it files. These tests run in milliseconds, and the baseline step is what makes each prove-it file readable on its own. |
| K8 | `test_chunker::test_defaults_are_380_460_and_60`, `test_parsed_document::test_the_default_text_cap_is_two_million_characters`, the ceiling assert in `test_upload_profiles::test_resolve_max_file_bytes` | They "only restate a constant", but each constant is a reviewed design or security limit. Changing one should take a deliberate edit in two places. |
| K9 | 25 tests with no `assert`, for example `test_branding::test_wcag_valid_colors`, the `_assert_*_denied` tests in `test_tenants_rls`/`test_document_limits`, and the constraint tests in `test_isolation`/`test_phase5_hardening`/`test_phase5_schema` | Each one fails by raising: a validator, a DB constraint, a socket guard or an `_assert_*` helper. None of them can always pass. |
| K10 | `test_upload_hardening` OpenXML tests (xlsx macros, embeddings, nested zip, OLE, external relationships, fail-closed XML) | `test_upload_profiles` checks the same rules mostly with the DOCUMENT profile. These tests are the TIMETABLE-profile (xlsx) guards. |
| K11 | `test_upload_hardening::test_child_failure_message_not_forwarded` | Its patch of `tpr.asyncio.to_thread` looked dead, but `tpr.asyncio` is the global `asyncio` module, so the patch reaches `sandbox.run_child`. The test is live. |
| K12 | `test_ml_loader_async.py` (1 test, its own file) | It is kept apart on purpose: `test_ml_loader.py` blocks sockets, and the Windows event loop needs a socket pair. |
| K13 | `test_review_5b3_deps.py`: the two "API imports no native parser" tests | They look alike, but one blocks the imports and the other runs with the libraries installed. |
| K14 | App functions used only by tests: `schedule.whats_on_now/todays_schedule/class_schedule`, `ml.loader.get_embedder_async/embed_query_async/embedder_is_loaded/reset_embedder_for_tests`, `upload_security.sniff_and_validate_file_type`, `body_limit.limit_for`, `authz.get_route_declared_permission`, `tpr._child_env` | Most are the planned API (`implementation_plan.md` §schedule intents, design A5 query embedding, design decision "one-line TIMETABLE wrapper"). The others are test hooks that the default-deny and body-limit guards depend on. |
| K15 | `app/scripts/bench_parse.py` (nothing imports it) | It is a documented operator tool (README, design doc, backlog B51). |

## After the cleanup (expected)

| Item | Before | After |
|---|---|---|
| Test items | 2,065 | 2,044 (−21) |
| Test functions | — | −21 from deletions, −5 more from the merges (the merges keep their items) |
| Files | 218 | 218 (no whole file is deleted) |
| Full-suite time | about 1,325 s | about 1,296 s (−6 s from deletions, −23 s from S1) |
| Coverage | 79% | expected unchanged; checked per file against the baseline |

## Where the time goes (not cleanup; proposed for the backlog)

- `test_ml_loader::test_the_worker_entry_point_still_refuses_to_start`: 60 s and a local failure (see Baseline).
- Timetable lifecycle requests cost about 1.2 s each (`test_archive_retention_keeps_last_five`: about
  16 requests in 19 s). That is a per-request cost in the app or its test setup, and it is worth profiling.
- `security/test_prove_it_fails_pipeline::test_with_row_by_row_inserts_...` (43 s) and
  `test_index_stage::..._1000_chunks` (18.5 s) are slow by design (1,000-row inserts).
- `test_timetable_hardening::test_redis_failure_between_acquire_and_release_does_not_lock_out` calls
  `pytest.skip` when Redis is down, but its file's tier (`subprocess`) says it needs no Redis.
- `tests/test_isolation.py` and `tests/test_timetable_lifecycle.py` start with a UTF-8 BOM (harmless; the other
  files don't have one).

## Questions before phase 3

- **Q1. Dead app code vs. rule 3.** All seven items in 1d are inside modules that `main.py` imports (directly or
  through a router), and rule 3 says never to touch such code. I read rule 3 as "do not delete modules that the
  app needs", not "never remove a dead function inside them". Delete 1d (yes/no)? If no, I list them in the
  backlog.
- **Q2.** Approve deletions D1–D10, the helpers in 1b, and the changes S1 and M1–M4 (all, or which ones)?
- **Q3.** Remove `orjson` and `jinja2` from `pyproject.toml`? Or keep `jinja2` for the planned email templates?

Answers (2026-11-19): Q1 yes, delete all seven; Q2 approve everything; Q3 remove both and add a backlog line.

---

# Phase 3 outcome (2026-11-19)

Nothing is committed. All proposals above were carried out, in batches. After each batch: ruff, mypy and the
tests of the touched files.

## What was changed, by batch

| Batch | Change | Check after the batch |
|---|---|---|
| 1. Dead app code (+D10) | Removed `get_request_id`, `get_request_tenant_slug`, `DayOfWeek`, `_check_and_increment_throttle`, `ImportRow.to_dict` (and the `Any` import it left unused), and the `_kill_tree` / `_describe_exit` re-exports. D10 (`test_describe_exit_reports_signal_name`) went with the `_describe_exit` re-export. 36 lines deleted in 5 files under `app/`. | `235 passed, 4 skipped, 1 xfailed in 119.65s (0:01:59)`; ruff and mypy clean |
| 2. Unused test helpers | Removed the 8 helpers of 1b (`PAD_ID`, `page_texts_of`, `raise_once`, `_login`, `embedded_tokens`, `_UNUSED`, `_set_tz`, `MAIN_PART`) and the imports they left unused. | `549 passed, 7 skipped, 1 deselected, 3 xfailed in 329.33s (0:05:29)` (the deselected test is the one fixed in the extra batch below) |
| 3. Duplicate tests | Removed D1–D9 (20 items; with D10 that is 21). | `442 passed, 7 skipped, 3 xfailed in 262.40s (0:04:22)`; ruff and mypy clean |
| 4. Shorten and merge | S1 (the per-IP throttle test now lowers the limit to 3 and does 4 logins: 25.3 s down to about 1 s), M1 (WCAG rejections), M2 (JWT secrets), M3 (next-lecture rollover), M4 (VBA project and embeddings). Item counts unchanged. | `105 passed in 22.77s`. **MC10 re-run against the shortened S1 test: `1 failed in 2.40s`**, so it still guards the rule |
| 5. Dependencies | Removed `orjson` and `jinja2` from `pyproject.toml`; backlog line B83 added. | Fresh venv: `pip install ".[dev]"` exit 0, `pip check`: "No broken requirements found.", neither package installed. In that venv: ruff `All checks passed!`, mypy `Success: no issues found in 91 source files`, gate tier `1353 passed, 11 skipped, 676 deselected, 4 xfailed in 391.76s (0:06:31)` |
| Extra. Worker-entry test + CLAUDE.md | `test_the_worker_entry_point_still_refuses_to_start` now starts the child with an explicitly empty `EMBEDDING_MODEL_DIR` and also asserts that stderr names it. CLAUDE.md now says to run the full suite once in the background instead of in two parts. | Three runs, all passed: this machine's own configuration (model configured) `1 passed in 1.69s`; parent environment `EMBEDDING_MODEL_DIR` empty `1 passed in 1.57s`; parent environment set to an existing directory `1 passed in 1.65s`. Before the fix, the configured machine hit the 60 s timeout |

**D3 (408 status):** the timetable parse timeout's **408 status check now runs only in the full tier**. Its
remaining test, `test_timetable_import.py::test_killable_parse_timeout_terminates_process`, is `db` and not
`security`, so the gate tier no longer asserts the 408. The gate still proves that a timeout kills the child
and its whole process tree (`test_sandbox.py`, `test_timetable_hardening::test_timeout_kills_whole_process_tree`).
Backlog row B85.

## Before and after

| Item | Before | After |
|---|---|---|
| Tracked files in `backend/` | 218 | 218 (no whole file deleted; new file `docs/cleanup-report.md` is outside `backend/`) |
| Test items collected | 2,065 | 2,044 (−21, as predicted) |
| Full run | `267 passed, 6 skipped, 1792 deselected in 677.86s` + `1 failed, 1775 passed, 12 skipped, 273 deselected, 4 xfailed in 647.21s` (1,325 s) | one run: `1 failed, 2021 passed, 18 skipped, 4 xfailed in 1350.62s (0:22:30)`, with coverage on |
| Time, like for like (both with coverage on) | 684.99 s + 690.04 s = 1,375.0 s | 1,350.6 s (−24 s) |
| ruff / mypy | `All checks passed!` / `Success: no issues found in 91 source files` | the same |
| Coverage of `app/` | 79% (9,828 statements, 2,085 missed) | 79% (9,815 statements, 2,081 missed) |
| `app/core` | 92.15% | 92.26% |
| `app/services` | 82.38% | 82.40% |
| `app/workers` | 93.01% | 93.10% |
| `app/ml` | 95.53% | 95.53% |

### Coverage differences, file by file (91 files compared)

Six files differ. None is a lost guard:

- `app/schemas/timetable.py`, `app/services/auth.py`, `app/services/invitation.py`,
  `app/services/timetable_parser_runner.py` (all −0.01 to −0.17 points): the percentage fell only because
  covered dead lines were deleted from the numerator and the denominator. The number of missed lines did not
  rise in any of them (`auth.py` and `invitation.py` even fell by one each). `app/core/context.py` rose from
  68.42% to 73.33% for the same reason.
- `app/workers/ingest.py`: +0.31 points (one more line covered; timing-dependent).
- `app/api/v1/documents.py` (an untouched file): −0.24 points, line 676 (the job-cancel step of document
  delete). It is a **measurement artifact**: line 675, the `if version_ids:` just above it, must run every time
  the route runs, yet coverage reports it as missed in the baseline, in the after run and in a single-test run;
  line 676 itself is reported missed even when run alone by `test_document_routes.py::test_delete_is_one_transaction_with_outbox_rows_and_subject_stays_blocked_until_purge`.
  No deleted test touches the document-delete route.

## The one failure in the final full run

`tests/security/test_prove_it_fails_review4.py::test_removing_the_one_job_per_tenant_rule_is_caught_by_the_round_helper`
failed once: `Failed: DID NOT RAISE AssertionError` (line 155). It passed in the baseline run, and then 8 of 8
runs alone and 2 of 2 runs of the whole `tests/security/` directory (80 tests each), so it is intermittent. I
changed nothing on this path (`workers/queue.py`, the test, or any test that runs before it). My explanation,
**not verified**: the test needs six simultaneous claims to really overlap in the database once the
one-job-per-tenant rule is removed; when they run one after another, the running cap still stops the second
claim and the helper has nothing to catch. I did not change the test. Backlog row B90. I did not repeat the
22-minute full run.

## Mistakes made during the cleanup (both caught and fixed)

- A `git checkout -- app/services/auth.py` to undo a mutation test also reverted my uncommitted batch-1
  deletion in that file. I noticed it, re-applied the deletion, and checked that `git diff -- app` showed
  exactly the 36 intended deletions before the final run.
- A first edit to `tests/test_document_route_matrix.py` added a stray comment instead of removing `_UNUSED`;
  it was corrected before any test ran, and the file's diff is 4 pure deletions.

## Left alone (also in `docs/backlog.md`, B83–B90)

`_request_id_var` / `_tenant_slug_var` (now write-only, B84), app code used only by tests (B89), the slow
tests that are not duplicates (B86), the stale "about 17 min" in the README (B87), small housekeeping (B88).
