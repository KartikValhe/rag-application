"""
tests/tier_map.py
─────────────────
The ONE reviewed table that assigns every test file to its tiers. conftest.py applies it in a collection
hook, so no test file needs markers of its own and a new test file cannot silently stay unclassified: a
file with no entry here makes collection fail with a message that says what to do.

Markers (all registered in pyproject.toml; `--strict-markers` is on):

  fast        needs no Postgres, no Redis and no child process. Runs anywhere, also with docker stopped.
              Enforced: a `fast` test that opens a database or Redis connection or starts a process fails
              (see `fast_tier_violation_patches` and the autouse fixture in conftest.py), so a test file
              cannot drift into the wrong tier unnoticed.
  db          needs Postgres and/or Redis (the dockerised test infrastructure).
  subprocess  starts child processes (the sandboxed parsers, `python -m ...` checks).
  security    tenant isolation, authorization, hostile-input and sandbox tests. Part of the gate tier.
  slow        the file takes 30 s or more on a developer machine (measured 2026-11-18, Windows).
  linux       added automatically to tests that are skipped off Linux (a `skipif` whose reason names
              Linux or POSIX). Their skips are unchanged: they run in CI, not on Windows.

Tiers as the runner (`python -m app.scripts.run_tests`) selects them:

  fast  = -m fast
  gate  = -m "fast or security"   (plus, with --touched, the test files of the changed modules)
  full  = everything

Rules for editing this table: exactly one of `fast`, or one or both of `db`/`subprocess`, per file. When in
doubt, use `db`: a wrongly slow tier costs time, a wrongly `fast` file fails loudly.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest

TIER_MARKERS = ("fast", "db", "subprocess", "security", "slow", "linux")
INFRA_MARKERS = ("db", "subprocess")

# Keys are paths relative to the tests directory, with forward slashes.
TEST_FILE_TIERS: dict[str, tuple[str, ...]] = {
    # ── security/ : the "prove it fails" meta-tests (each one breaks a defence on purpose and checks the
    #    assertion helper notices). Most need the database; two are pure.
    "security/test_prove_it_fails_documents.py": ("db", "security"),
    "security/test_prove_it_fails_parse.py": ("db", "subprocess", "security"),
    "security/test_prove_it_fails_pipeline.py": ("db", "security", "slow"),
    "security/test_prove_it_fails_preauth.py": ("db", "security"),
    "security/test_prove_it_fails_review2.py": ("fast", "security"),
    "security/test_prove_it_fails_review4.py": ("db", "security"),
    "security/test_prove_it_fails_retrieval.py": ("db", "security"),
    "security/test_prove_it_fails_scanner.py": ("db", "security"),
    "security/test_prove_it_fails_review5b3.py": ("db", "subprocess", "security"),
    "security/test_prove_it_fails_uploads.py": ("fast", "security"),
    "security/test_prove_it_fails_worker.py": ("db", "security"),
    "security/test_prove_it_fails_usage.py": ("db", "security"),  # 6a-5: RLS alone, wider grants, missing FORCE on 0012
    "security/test_prove_it_fails_chat_rls.py": ("db", "security"),  # 6a-8: owner policy, FORCE, grants, pending policy, reply index, queue on 0013
    # ── tests that need no infrastructure ──
    "test_body_limit_routes.py": ("fast", "security"),
    "test_chunker.py": ("fast",),
    "test_document_extract.py": ("fast",),
    "test_ingest_settings.py": ("fast",),
    "test_document_text.py": ("fast",),
    "test_injection_scan.py": ("fast", "security"),
    "test_llm_types.py": ("fast", "security"),  # 6a-2: request/event/error types, no tool fields, fixed error text
    "test_llm_fake.py": ("fast",),  # 6a-2: the scripted fake provider and its call log
    "test_llm_models_file.py": ("fast",),  # 6a-2: models.json schema validation (unknown keys, pins, trains_on_inputs)
    "test_llm_settings.py": ("fast", "security"),  # 6a-2: LLM_*/CHAT_* settings, provider keys never shown
    "test_llm_policy.py": ("fast", "security"),  # 6a-2: allow-list and start-up policy (training, Groq ZDR, chain keys)
    "test_llm_no_network.py": ("fast", "security"),  # 6a-2: provider-host socket guard, no vendor SDK in app/
    "test_llm_sse.py": ("fast",),  # 6a-3: the SSE line parser (split bytes, CRLF, comments, limits)
    "test_llm_adapters.py": ("fast", "security"),  # 6a-3: Groq/OpenRouter/Gemini adapters on httpx.MockTransport (events, errors, timeouts, key canary, no tool fields)
    "test_llm_adapters_recorded.py": ("fast",),  # 6a-6b: the Groq adapter on the 8 real recorded streams (text, reasoning dropped, reported usage, done)
    "test_llm_logging.py": ("fast", "security"),  # 6a-3: credential keys dropped from logs, httpx loggers at WARNING
    "test_llm_models_pinned.py": ("fast",),  # 6a-3: pinned models.json entries and their sources in the build record
    "test_llm_chain.py": ("db",),  # 6a-4: retries, fallback, breaker in the chain, cooldowns, RPM/RPD slots, timeouts (FakeLLM, test Redis)
    "test_llm_breaker.py": ("db",),  # 6a-4: breaker open/half-open/probe and cooldown scripts on the test Redis, fail closed
    "test_llm_chain_imports.py": ("fast", "security"),  # 6a-4: no module but the chain imports an adapter (source scan)
    "test_llm_budget_pure.py": ("fast",),  # 6a-5: token estimate, budget settings, override rule, keys (no Redis)
    "test_llm_budget.py": ("db",),  # 6a-5: daily token counters on the test Redis: reserve, correct, exhausted, overlap
    "test_llm_usage.py": ("db",),  # 6a-5: record_usage writes one content-free llm_usage_events row
    "test_llm_recording.py": ("fast", "security"),  # 6a-6: dev-only SSE recording hook in transport.py (off by default, body lines only, no key)
    "test_llm_smoke.py": ("fast", "security"),  # 6a-6: llm_smoke script on a stub chain + FakeLLM (dev only, key canary, out folder, 429 wait, resume)
    "test_llm_smoke_data.py": ("fast",),  # 6a-6: shape of the SAMPLE sources and questions (en/mr)
    "test_llm_smoke_chain.py": ("db",),  # 6a-6: llm_smoke through the real LLMChain on the test Redis (cooldown wait)
    "test_llm_tpm.py": ("db",),  # 6a-10b: the token-weighted TPM window on the test Redis: one atomic script, settle, expiry, oversize, forced overlap
    "test_llm_chain_tpm.py": ("db",),  # 6a-10b: the TPM window in the chain: weight, correction, refund, own limit per model, skip not wait, Redis down
    "test_chat_router.py": ("fast",),  # 6a-7: pure rule router (intents en/mr, look-alikes, names only from the caller's scope, total on any string, no I/O imports)
    "test_chat_language.py": ("fast",),  # 6a-7: Devanagari share, mr/hi function words, tie gives the tenant default, pure
    "test_tenant_settings.py": ("db",),  # 6a-5: one tenant_settings row per tenant, written on tenant creation
    "test_phase6_schema.py": ("db", "security"),  # 6a-5/6a-8: migrations 0012 and 0013: FORCE RLS, policies, grants per role, FKs, down/up
    "test_chat_migration_0013.py": ("fast", "security"),  # 6a-8: the SQL 0013 issues (fake op), where app_chat_maint is created
    "test_chat_isolation.py": ("db", "security"),  # 6a-8: owner/tenant isolation, maint role, queue grants as the real roles
    "test_chat_session_context.py": ("db", "security"),  # 6a-9a: chat_session_context sets tenant and user locally, fails closed
    "test_chat_source_scans.py": ("fast", "security"),  # 6a-9a: maintenance URL/module worker-only, retired chat_maintenance setting gone
    "test_chat_api.py": ("db",),  # 6a-9a: conversation routes (create/list/get/delete), limits, chat_disabled, no held connection
    "test_chat_route_matrix.py": ("db", "security"),  # 6a-9a: every chat route x role, generated from the route table
    "test_chat_sse.py": ("fast",),  # 6a-9b: SSE event bytes, one-line JSON, heartbeat helper and stream deadline
    "test_chat_stream_filter.py": ("fast", "security"),  # 6a-10a: verdict gate on prefixes, label filter, 【S#】/U+2011 normalisation, caps, leak cut, real recordings
    "test_chat_prompt.py": ("fast", "security"),  # 6a-10a: rag-v1 prompt, random SOURCES boundary, neutralising, no ids, flagged marking, budget, history
    "test_chat_citations.py": ("fast", "security"),  # 6a-10a: server-built citation snapshots, location kinds, snippets, student fields only
    "test_pii.py": ("fast", "security"),  # 6a-10a: PII masking of what goes to the model (e-mail, phones, ID numbers, own name)
    "test_guardrails.py": ("fast",),  # 6a-10a: Phase 7 checkpoint stubs with the two real checks (input length, no context)
    "test_chat_message_api.py": ("db", "security"),  # 6a-9b: message route check order, T1/T3, idempotency, limits, cancel, roles, logs
    "test_chat_schedule.py": ("db",),  # 6a-9b: schedule path end to end per role, templates en/mr, zero model and retrieval calls
    "test_chat_rag.py": ("db", "security"),  # 6a-10c: the wired RAG turn end to end (not found, filter, citations, budget, fallback, T3, recordings, cancel, pool, logs)
    "test_rag_answer_settings.py": ("fast",),  # 6a-10c: tuning settings and their .env.example lines, the RAG flag, named constants
    "test_chat_condense_pure.py": ("fast",),  # 6a-11: follow-up detection, the condense request (masked, no tools), untrusted output cleaning
    "test_chat_condense.py": ("db", "security"),  # 6a-11: condense in the turn: retrieval only, budget, usage rows, every failure falls back
    "test_chat_narrowing.py": ("db", "security"),  # 6a-11: tenant allowed_llm_providers in the turn (canary never called), one usage row per failed attempt
    "test_llm_chain_narrowing.py": ("db", "security"),  # 6a-11: the chain narrowed by the tenant context, attempt numbers and latency
    "test_chat_history.py": ("db", "security"),  # 6a-12a: history route: owner only, scope re-check, student citation shape, stale pending, paging, cursor 422
    "test_chat_feedback.py": ("db", "security"),  # 6a-12a: feedback upsert/delete on the caller's own answers only, cleaned comment, no audit, no logs
    "test_chat_settings_pure.py": ("fast", "security"),  # 6a-12a: settings validation (exact provider names, 6a-5 budget codes, unknown fields) and the change record
    "test_chat_settings_api.py": ("db", "security"),  # 6a-12a: settings GET (creates nothing) and PATCH (upsert, audited changes, tenant only)
    "test_chat_maintenance_pure.py": ("fast", "security"),  # 6a-12b: the maintenance URL's user, null/invalid retention refused before any connection, settings and guard list
    "test_chat_retention_migration_0014.py": ("fast", "security"),  # 6a-12b: migration 0014 (nullable retention, 30 to 3650 or null) never changes a row; refusals
    "test_chat_retention.py": ("db", "security"),  # 6a-12b: retention only through app_chat_maint, null keeps everything, usage events, stale pending, blank queue and the purge outbox
    "test_sse_transport.py": ("db",),  # 6a-9b: F11, stream not buffered and disconnect reaching the generator through the stack
    "test_ml_fetch_model.py": ("fast",),
    "test_ml_loader_async.py": ("fast",),
    "test_query_embedding.py": ("fast",),  # 6a-1b: the shared guarded query embedder and the start-up warm-up flag
    "test_ml_real_model.py": ("fast",),  # skipped unless RUN_REAL_MODEL=1 (needs the real model files)
    "test_ml_scripts.py": ("fast",),
    "test_parsed_document.py": ("fast",),
    "test_document_version_updated_at.py": ("fast",),  # the response field and the source guard for writers
    "test_document_usage_keys.py": ("fast", "security"),  # the Redis keys of uploads_last_hour come from the user only
    "test_phase5_doc_layout.py": ("fast",),  # section 11 numbering of docs/api/phase5.md (the drift guard reads it)
    "test_pdf_embedded_file_rule.py": ("fast", "security"),
    "test_retrieval_eval_metrics.py": ("fast",),
    "test_eval_retrieval_script.py": ("fast",),
    "test_retrieval_pure.py": ("fast",),
    "test_retrieval_fusion_pure.py": ("fast",),
    "test_retrieval_compare_pure.py": ("fast",),
    "test_retrieval_calibration.py": ("fast",),  # 5d-5: defaults, derived threshold_calibrated, startup line
    "test_golden_set_fixture.py": ("fast",),  # 5d-5: the committed golden set parses and is large enough
    "test_review_5b3_text.py": ("fast",),
    "test_review_5b3_upload_scan.py": ("fast", "security"),
    "test_run_tests.py": ("fast",),
    "test_schedule.py": ("fast",),
    "test_test_db_guard.py": ("fast", "security"),
    "test_tiers.py": ("fast",),
    "test_upload_profiles.py": ("fast",),
    "test_worker_entry.py": ("fast", "security"),
    "test_upload_review_followups.py": ("fast", "security"),
    # ── tests that start child processes (no database) ──
    "test_document_parse_child.py": ("subprocess",),
    "test_ml_embedder.py": ("subprocess",),
    "test_ml_loader.py": ("subprocess",),
    "test_review_5b3_child.py": ("subprocess", "security"),
    "test_review_5b3_deps.py": ("subprocess", "security"),
    "test_review_5b3_parent.py": ("subprocess", "security"),
    "test_sandbox.py": ("subprocess", "security", "slow"),
    "test_sandbox_isolation.py": ("subprocess", "security"),
    "test_timetable_hardening.py": ("subprocess", "security"),
    "test_timetable_real_child.py": ("subprocess",),
    # ── tests that need both the database and child processes ──
    "test_ingest_pipeline_e2e.py": ("db", "subprocess", "slow"),
    "test_ingest_worker.py": ("db", "subprocess", "slow"),
    "test_parse_stage.py": ("db", "subprocess", "slow"),
    "test_timetable_import.py": ("db", "subprocess"),
    "test_upload_redos.py": ("db", "subprocess", "security"),
    # ── tests that need Postgres and/or Redis ──
    "test_academic_world.py": ("db",),
    "test_admin.py": ("db",),
    "test_auth.py": ("db",),
    "test_auth_released_session.py": ("db", "security"),  # 6a-1a (B100): no pooled connection held after the check
    "test_authz_rbac.py": ("db", "security"),
    "test_batch_c.py": ("db",),
    "test_branding.py": ("db",),
    "test_document_lifecycle.py": ("db", "slow"),
    "test_document_version_updated_at_db.py": ("db",),  # routes carry updated_at; it moves on every transition
    "test_document_limits.py": ("db",),
    "test_document_versions_list.py": ("db",),  # GET /documents/{id}/versions: order, cut at 100, shape
    "test_document_versions_list_security.py": ("db", "security"),  # same generic 404, roles, RLS for the list (gate)
    "test_document_usage_counts.py": ("db",),  # usage route: versions_by_status, uploads_last_hour, Redis down
    "test_document_usage_isolation.py": ("db", "security"),  # RLS for the counts, user-only Redis keys (gate)
    "test_document_limits_route.py": ("db", "security"),  # who may read the limits, and no leak between tenants (gate)
    "test_document_retryable.py": ("db",),  # one retry rule for the status route and the version responses
    "test_embed_stage.py": ("db", "slow"),
    "test_document_flags_route.py": ("db", "security"),  # who may read the flags (gate)
    "test_document_flags_contract.py": ("db",),  # keys, excerpts, spans, paging, read-only
    "test_document_permissions.py": ("db", "security"),
    "test_document_retry_limits.py": ("db",),
    "test_document_route_matrix.py": ("db", "security", "slow"),
    "test_document_routes.py": ("db", "slow"),
    "test_document_scope.py": ("db", "security"),
    "test_document_store.py": ("db",),
    "test_document_upload.py": ("db", "slow"),
    "test_engine_config.py": ("db", "security"),
    "test_index_stage.py": ("db", "slow"),
    "test_ingest_sweeper.py": ("db",),
    "test_isolation.py": ("db", "security"),
    "test_phase2_endpoints.py": ("db",),
    "test_phase5_hardening.py": ("db", "security"),
    "test_phase5_schema.py": ("db",),
    "test_platform.py": ("db",),
    "test_retrieval_devanagari.py": ("db",),
    "test_retrieval_flags.py": ("db", "security"),
    "test_retrieval_grants.py": ("db", "security"),
    "test_retrieval_leaks.py": ("db", "security"),
    "test_retrieval_limits.py": ("db", "security"),
    "test_retrieval_negative.py": ("db", "security"),
    "test_retrieval_quality.py": ("db",),
    "test_eval_retrieval_db.py": ("db",),
    "test_retrieval_pruning.py": ("db",),
    "test_retrieval_file_kind.py": ("db",),  # 6a-1b: Hit.file_kind for pdf/pptx/docx; test-search contract unchanged
    "test_retrieval_calibration_db.py": ("db",),  # 5d-5: threshold_calibrated derived at both entry points
    "test_eval_retrieval_compare_db.py": ("db",),
    "test_retrieval_scope.py": ("db", "security"),
    "test_retrieval_sql_safety.py": ("db", "security"),
    "test_security_headers_layer.py": ("db", "security"),
    "test_security_review.py": ("db", "security", "slow"),
    "test_slots_renew.py": ("db",),
    "test_teacher_class_scope.py": ("db", "security"),
    "test_test_search_route.py": ("db", "security"),  # who may search, scope, rate-limit order (gate)
    "test_test_search_contract.py": ("db",),  # the response contract: 422s, k, 503s, cap, logs, headers
    "test_rescan_flags.py": ("db",),
    "test_validation_errors.py": ("db", "security"),
    "test_tenants_rls.py": ("db", "security"),
    "test_throttle.py": ("db", "security"),
    "test_timetable_conflicts.py": ("db",),
    "test_timetable_endpoints.py": ("db", "slow"),
    "test_timetable_lifecycle.py": ("db", "slow"),
    "test_upload_cleanup.py": ("db",),
    "test_upload_hardening.py": ("db", "security"),
    "test_upload_preauth.py": ("db", "security"),
    "test_worker_fair_claim.py": ("db",),
    "test_worker_queue.py": ("db",),
    "test_worker_review_fixes.py": ("db", "slow"),
    "test_worker_tenant_guard.py": ("db", "security"),
}

UNMAPPED_HELP = (
    "Every test file needs a tier. Add the file to TEST_FILE_TIERS in tests/tier_map.py: "
    "'fast' if it needs no Postgres, Redis or child process, otherwise 'db' and/or 'subprocess'; add "
    "'security' for isolation, authorization, hostile-input or sandbox tests, and 'slow' if it takes "
    "30 s or more."
)


def discover_test_files(tests_root: Path) -> list[str]:
    """All test files under the tests directory, as paths relative to it (forward slashes)."""
    return sorted(p.relative_to(tests_root).as_posix() for p in tests_root.rglob("test_*.py"))


def table_problems(
    files: Iterable[str], table: dict[str, tuple[str, ...]] | None = None
) -> list[str]:
    """Everything wrong with the table: unmapped files, stale entries, bad or contradictory markers."""
    table = TEST_FILE_TIERS if table is None else table
    on_disk = set(files)
    problems: list[str] = []
    for rel in sorted(on_disk - set(table)):
        problems.append(f"{rel}: test file has no entry in TEST_FILE_TIERS")
    for rel in sorted(set(table) - on_disk):
        problems.append(f"{rel}: entry in TEST_FILE_TIERS but no such test file")
    for rel, markers in sorted(table.items()):
        unknown = [m for m in markers if m not in TIER_MARKERS or m == "linux"]
        if unknown:
            problems.append(f"{rel}: unknown or automatic marker(s) {unknown}")
        if len(set(markers)) != len(markers):
            problems.append(f"{rel}: duplicate marker")
        infra = [m for m in markers if m in INFRA_MARKERS]
        if "fast" in markers and infra:
            problems.append(f"{rel}: 'fast' cannot be combined with {infra}")
        if "fast" not in markers and not infra:
            problems.append(f"{rel}: needs 'fast' or at least one of {list(INFRA_MARKERS)}")
    return problems


def _is_linux_only(item: Any) -> bool:
    """True for a test that is skipped off Linux: a skipif whose reason names Linux or POSIX."""
    for mark in item.iter_markers("skipif"):
        reason = str(mark.kwargs.get("reason", "")).lower()
        if "linux" in reason or "posix" in reason:
            return True
    return False


def apply_tiers(
    items: Sequence[Any], tests_root: Path, touched: frozenset[str] = frozenset()
) -> None:
    """Add the tier markers to every collected item, or fail collection if any file has no entry."""
    unmapped: set[str] = set()
    for item in items:
        path = Path(str(item.path))
        try:
            rel = path.resolve().relative_to(tests_root.resolve()).as_posix()
        except ValueError:
            rel = path.name
        markers = TEST_FILE_TIERS.get(rel)
        if markers is None:
            unmapped.add(rel)
            continue
        for name in markers:
            item.add_marker(getattr(pytest.mark, name))
        if rel in touched:
            item.add_marker(pytest.mark.touched)
        if _is_linux_only(item):
            item.add_marker(pytest.mark.linux)
    if unmapped:
        listing = "\n".join(f"  tests/{rel}" for rel in sorted(unmapped))
        raise pytest.UsageError(f"Test file(s) without a tier:\n{listing}\n{UNMAPPED_HELP}")


def fast_tier_violation_patches() -> list[tuple[Any, str, Any]]:
    """(object, attribute, replacement) triples that make a `fast` test fail loudly when it opens a
    database or Redis connection or starts a child process. Applied by the autouse fixture in conftest."""
    import asyncio.base_events
    import subprocess

    import asyncpg
    import asyncpg.connection
    import redis.asyncio.connection as redis_async_connection
    import redis.connection as redis_connection

    class FastTierViolation(AssertionError):
        pass

    def refuse(kind: str) -> Any:
        def _refuse(*_args: object, **_kwargs: object) -> Any:
            raise FastTierViolation(
                f"a test marked 'fast' tried to use {kind}. Change its entry in tests/tier_map.py "
                f"(db / subprocess), or remove the {kind} use from the test."
            )

        return _refuse

    return [
        (subprocess.Popen, "__init__", refuse("a child process")),
        (asyncio.base_events.BaseEventLoop, "subprocess_exec", refuse("a child process")),
        (asyncio.base_events.BaseEventLoop, "subprocess_shell", refuse("a child process")),
        (asyncpg, "connect", refuse("a Postgres connection")),
        (asyncpg, "create_pool", refuse("a Postgres connection")),
        (asyncpg.connection, "connect", refuse("a Postgres connection")),
        (redis_async_connection.AbstractConnection, "connect", refuse("a Redis connection")),
        (redis_connection.AbstractConnection, "connect", refuse("a Redis connection")),
    ]
