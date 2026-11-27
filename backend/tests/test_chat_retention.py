"""
tests/test_chat_retention.py
───────────────────────────
Step 6a-12b, worker maintenance (docs/phase6-plan.md 9.2, 9.4, 9.6, 11.1; owner decisions Q4 and C35 option a):

  - EVERY maintenance transaction runs as app_chat_maint and refuses anything else (superuser, app_tenant);
  - retention: conversations whose last message is older than the tenant's retention_days go, for EVERY user of the
    tenant, with their messages, citations and feedback, in small batches; the audit row holds counts only;
  - Q4: a NULL retention (and a tenant with no row) means keep everything: nothing is deleted, not a conversation and
    not a usage event, and no audit row is written; a dry run counts without deleting;
  - old llm_usage_events go after LLM_USAGE_RETENTION_DAYS, only for a tenant that has a retention period;
  - stale pending answers become interrupted (fake clock), fresh ones and finished ones do not, for every tenant;
  - the outbox: the purge route and the sweeper's tombstone purge each insert one queue row in their own transaction;
    the sweeper blanks the snippet and heading of that document's citations of ALL users and of nothing else, deletes the
    queue row, and a run that fails before the commit leaves the row so the next run repeats it;
  - one tenant's failure does not stop another; a sweeper without the maintenance object leaves chat alone.
The retention value is read through an app_tenant session: app_chat_maint has no grant on tenant_settings.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.core.config import settings
from app.services import chat_maintenance
from app.services.chat_maintenance import ChatMaintenance, MaintenanceRoleError, RetentionCounts
from app.workers.db import TenantRef, WorkerDatabases
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import auth, make_document
from tests.fixtures.maint_world import (  # noqa: F401  (pytest fixtures)
    CANARY,
    CLOCK_START,
    audit_rows,
    cite,
    count,
    make_maint_engine,
    make_maintenance,
    make_sweeper_with,
    old_conversation,
    pending_answer,
    set_retention,
    usage_event,
)
from tests.fixtures.worker import FakeClock

NOW = CLOCK_START


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(CLOCK_START)


@pytest_asyncio.fixture
async def maint_engine() -> AsyncIterator[AsyncEngine]:
    engine = make_maint_engine()  # runs as app_chat_maint (SET ROLE on connect)
    yield engine
    await engine.dispose()


def days_ago(days: float, seconds: float = 0) -> datetime:
    return NOW - timedelta(days=days, seconds=seconds)


async def sweep(dbs: WorkerDatabases, tmp_path: Path, maint: ChatMaintenance | None, clock: FakeClock, *tenants: Any) -> Any:
    report = await make_sweeper_with(dbs, tmp_path, maint, clock, {t.id for t in tenants}).run_once()
    assert report.ran
    return report


# ── the role ──────────────────────────────────────────────────────────────────


async def test_the_maintenance_engine_runs_as_app_chat_maint(maint_engine: AsyncEngine, clock: FakeClock) -> None:
    maint = make_maintenance(maint_engine, clock)
    await maint.verify_role()  # no exception
    async with maint_engine.connect() as conn:
        assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "app_chat_maint"


async def test_every_maintenance_transaction_refuses_a_superuser_and_app_tenant_and_changes_nothing(
    academic_world: AcademicWorld, raw_db: AsyncSession, clock: FakeClock
) -> None:
    w = academic_world.a
    await set_retention(raw_db, w, 30)
    await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(45))
    stale = await pending_answer(raw_db, w, w.s1, (await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(1)))[0],
                                 created_at=NOW - timedelta(hours=1))
    ref = TenantRef(w.id)
    wrong: list[AsyncEngine] = [make_maint_engine(None), create_async_engine(os.environ["DATABASE_URL"], connect_args={"statement_cache_size": 0})]
    try:
        for engine in wrong:
            maint = make_maintenance(engine, clock)
            with pytest.raises(MaintenanceRoleError):
                await maint.verify_role()
            for call in (
                lambda m=maint: m.mark_stale_pending(ref), lambda m=maint: m.delete_expired(ref, 30),
                lambda m=maint: m.count_retention_due(ref, 30), lambda m=maint: m.drain_blank_queue(ref),
            ):
                with pytest.raises(MaintenanceRoleError):
                    await call()
    finally:
        for engine in wrong:
            await engine.dispose()
    assert await count(raw_db, "conversations", w.id) == 2
    await raw_db.refresh(stale)
    assert stale.outcome == "pending"


# ── retention ─────────────────────────────────────────────────────────────────


async def test_expired_conversations_of_every_user_go_with_their_children_and_fresh_ones_stay(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None, caplog: pytest.LogCaptureFixture,
) -> None:
    w = academic_world.a
    await set_retention(raw_db, w, 30)
    student_old, student_rows = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(45))
    teacher_old, teacher_rows = await old_conversation(raw_db, w, w.t1, last_message_at=days_ago(31), exchanges=2)
    just_over, just_over_rows = await old_conversation(raw_db, w, w.s2, last_message_at=days_ago(30, seconds=1))
    exactly, _ = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(30))
    fresh, fresh_rows = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(10))
    await cite(raw_db, w, w.s1, student_rows[1], uuid.uuid4())
    await raw_db.execute(
        text("INSERT INTO app.message_feedback (tenant_id, message_id, user_id, rating) VALUES (:t, :m, :u, 1)"),
        {"t": w.id, "m": student_rows[1].id, "u": w.s1.id},
    )
    await cite(raw_db, w, w.s1, fresh_rows[1], uuid.uuid4())
    await raw_db.commit()

    caplog.set_level(logging.DEBUG)
    report = await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, w)

    left = {c for (c,) in (await raw_db.execute(text("SELECT id FROM app.conversations WHERE tenant_id = :t"), {"t": w.id})).all()}
    assert left == {exactly.id, fresh.id}, "strictly older than the period goes; exactly the period stays"
    assert await count(raw_db, "messages", w.id) == 4, "only the messages of the two kept conversations are left"
    assert await count(raw_db, "message_citations", w.id) == 1 and await count(raw_db, "message_feedback", w.id) == 0
    assert (report.conversations_deleted, report.messages_deleted) == (3, 8)

    rows = await audit_rows(raw_db, w.id, "chat:retention_purge")
    assert len(rows) == 1
    actor, target_type, target_id, details = rows[0]
    assert (actor, target_type, target_id) == (None, "tenant", str(w.id))
    assert details == {"retention_days": 30, "conversations": 3, "messages": 8, "usage_events": 0, "stale_pending_marked": 0}
    assert CANARY not in str(details) and CANARY not in caplog.text


async def test_the_second_run_finds_nothing_and_writes_no_second_audit_row(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    w = academic_world.a
    await set_retention(raw_db, w, 30)
    await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(45))
    maint = make_maintenance(maint_engine, clock)
    await sweep(worker_dbs, tmp_path, maint, clock, w)
    second = await sweep(worker_dbs, tmp_path, maint, clock, w)
    assert (second.conversations_deleted, second.messages_deleted, second.usage_events_deleted) == (0, 0, 0)
    assert len(await audit_rows(raw_db, w.id, "chat:retention_purge")) == 1


async def test_nothing_is_deleted_for_a_tenant_whose_retention_is_null_or_that_has_no_row(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    """Q4 (owner): the default is keep everything. A NULL row and a missing row, with five-year-old chats and ten-year-old
    usage events: every row is still there afterwards, nothing is counted and nothing is audited."""
    a, b = academic_world.a, academic_world.b
    await set_retention(raw_db, a, None)  # a row holding NULL
    assert await count(raw_db, "tenant_settings", b.id) == 0  # b: no row at all
    for w, user in ((a, a.s1), (b, b.s1)):
        await old_conversation(raw_db, w, user, last_message_at=days_ago(5 * 365), exchanges=2)
        await old_conversation(raw_db, w, user, last_message_at=days_ago(100))
        await usage_event(raw_db, w, created_at=days_ago(10 * 365))
    before = {(t, w.id): await count(raw_db, t, w.id) for w in (a, b) for t in ("conversations", "messages", "llm_usage_events")}

    maint = make_maintenance(maint_engine, clock)
    report = await sweep(worker_dbs, tmp_path, maint, clock, a, b)

    assert {(t, w.id): await count(raw_db, t, w.id) for w in (a, b) for t in ("conversations", "messages", "llm_usage_events")} == before
    assert (report.conversations_deleted, report.messages_deleted, report.usage_events_deleted) == (0, 0, 0)
    for w in (a, b):
        assert await audit_rows(raw_db, w.id, "chat:retention_purge") == []
    # The row still holds NULL (nothing wrote a default back) and the table still has no row for b.
    assert await count(raw_db, "tenant_settings", b.id) == 0


async def test_a_tenant_made_by_the_defaults_path_keeps_everything(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    from app.core.tenant_db import tenant_session_context
    from app.services import tenant_settings

    w = academic_world.a
    async with tenant_session_context(str(w.id)) as db:
        await tenant_settings.create_default_settings(db, w.id)
    await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(2000))
    await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, w)
    assert await count(raw_db, "conversations", w.id) == 1


async def test_the_dry_run_counts_what_would_go_and_deletes_nothing(
    academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine, clock: FakeClock
) -> None:
    w = academic_world.a
    await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(45))
    await old_conversation(raw_db, w, w.t1, last_message_at=days_ago(40), exchanges=2)
    await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(5))
    await usage_event(raw_db, w, created_at=days_ago(401))
    await usage_event(raw_db, w, created_at=days_ago(399))
    maint = make_maintenance(maint_engine, clock)
    due = await maint.count_retention_due(TenantRef(w.id), 30)
    assert due == RetentionCounts(conversations=2, messages=6, usage_events=1)
    assert await count(raw_db, "conversations", w.id) == 3 and await count(raw_db, "messages", w.id) == 8
    assert await count(raw_db, "llm_usage_events", w.id) == 2
    assert await maint.count_retention_due(TenantRef(w.id), None) == RetentionCounts()


async def test_it_deletes_in_small_batches_and_a_run_stops_after_its_batch_limit(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    w = academic_world.a
    await set_retention(raw_db, w, 30)
    for i in range(5):
        await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(100 + i))
    small = make_maintenance(maint_engine, clock, batch_size=2, max_batches=1)
    sizes = []
    for _ in range(3):
        sizes.append((await sweep(worker_dbs, tmp_path, small, clock, w)).conversations_deleted)
    assert sizes == [2, 2, 1], "two per batch, one batch per run: the rest waits for the next run"
    assert await count(raw_db, "conversations", w.id) == 0

    for i in range(5):
        await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(100 + i))
    many = make_maintenance(maint_engine, clock, batch_size=2, max_batches=50)
    assert (await sweep(worker_dbs, tmp_path, many, clock, w)).conversations_deleted == 5


async def test_a_run_that_fails_after_some_batches_still_counts_and_audits_what_it_deleted(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleted data is never left out of the audit trail: batch 1 committed, batch 2 failed -> one conversation gone,
    and the audit row and the report say exactly one."""
    w = academic_world.a
    await set_retention(raw_db, w, 30)
    for i in range(3):
        await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(100 + i))
    real = ChatMaintenance._delete_conversation_batch
    calls = {"n": 0}

    async def second_batch_fails(self: ChatMaintenance, ref: TenantRef, days: int) -> tuple[int, int]:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the second batch fails")
        return await real(self, ref, days)

    monkeypatch.setattr(ChatMaintenance, "_delete_conversation_batch", second_batch_fails)
    report = await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock, batch_size=1), clock, w)
    assert await count(raw_db, "conversations", w.id) == 2
    assert (report.conversations_deleted, report.messages_deleted) == (1, 2)
    (row,) = await audit_rows(raw_db, w.id, "chat:retention_purge")
    assert row[3]["conversations"] == 1 and row[3]["messages"] == 2


# ── usage events ──────────────────────────────────────────────────────────────


async def test_old_usage_events_go_only_for_a_tenant_with_a_period_and_each_tenant_keeps_its_own(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    a, b = academic_world.a, academic_world.b
    await set_retention(raw_db, a, 30)
    await set_retention(raw_db, b, None)
    old_a = await usage_event(raw_db, a, created_at=days_ago(401))
    new_a = await usage_event(raw_db, a, created_at=days_ago(399))
    old_b = await usage_event(raw_db, b, created_at=days_ago(900))
    report = await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, a, b)
    kept = {r for (r,) in (await raw_db.execute(text("SELECT id FROM app.llm_usage_events WHERE tenant_id = ANY(:t)"), {"t": [a.id, b.id]})).all()}
    assert kept == {new_a, old_b} and old_a not in kept
    assert report.usage_events_deleted == 1
    (row,) = await audit_rows(raw_db, a.id, "chat:retention_purge")
    assert row[3]["usage_events"] == 1


# ── stale pending ─────────────────────────────────────────────────────────────


async def test_stale_pending_answers_become_interrupted_and_nothing_else_changes(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    a, b = academic_world.a, academic_world.b
    await set_retention(raw_db, a, None)  # marking is not a retention delete: it also runs for a keep-everything tenant
    conv_a, rows_a = await old_conversation(raw_db, a, a.s1, last_message_at=days_ago(1))
    stale = await pending_answer(raw_db, a, a.s1, conv_a, created_at=NOW - timedelta(seconds=121))
    fresh = await pending_answer(raw_db, a, a.s1, conv_a, created_at=NOW - timedelta(seconds=60))
    conv_b, _ = await old_conversation(raw_db, b, b.s1, last_message_at=days_ago(1))
    other_tenant = await pending_answer(raw_db, b, b.s1, conv_b, created_at=NOW - timedelta(hours=5))

    report = await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, a)  # only tenant a

    for row in (stale, fresh, other_tenant, rows_a[1]):
        await raw_db.refresh(row)
    assert (stale.outcome, stale.partial, stale.content) == ("interrupted", True, "")
    assert stale.completed_at == NOW
    assert (fresh.outcome, fresh.partial, fresh.completed_at) == ("pending", False, None)
    assert other_tenant.outcome == "pending", "a tenant that is not swept is untouched"
    assert (rows_a[1].outcome, rows_a[1].partial) == ("answered", False), "a finished answer is never touched"
    questions = (await raw_db.execute(text("SELECT count(*) FROM app.messages WHERE tenant_id = :t AND role = 'user' AND outcome IS NOT NULL"), {"t": a.id})).scalar_one()
    assert questions == 0 and report.stale_marked == 1
    (row,) = await audit_rows(raw_db, a.id, "chat:retention_purge")
    assert row[3] == {"retention_days": None, "conversations": 0, "messages": 0, "usage_events": 0, "stale_pending_marked": 1}

    clock.advance(70)  # now the 60-second-old one is 130 s old
    again = await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, a)
    assert again.stale_marked == 1
    await raw_db.refresh(fresh)
    assert fresh.outcome == "interrupted"


# ── the blank queue (outbox) ──────────────────────────────────────────────────


async def _queue(raw_db: AsyncSession, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    await raw_db.commit()
    return [d for (d,) in (await raw_db.execute(
        text("SELECT document_id FROM app.chat_citation_blank_queue WHERE tenant_id = :t ORDER BY created_at, id"), {"t": tenant_id})).all()]


async def _enqueue(raw_db: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID) -> None:
    await raw_db.execute(text("INSERT INTO app.chat_citation_blank_queue (tenant_id, document_id) VALUES (:t, :d)"), {"t": tenant_id, "d": document_id})
    await raw_db.commit()


async def test_the_sweeper_blanks_the_citations_of_the_purged_document_for_every_user_and_nothing_else(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None, caplog: pytest.LogCaptureFixture,
) -> None:
    a, b = academic_world.a, academic_world.b
    doc, other_doc = uuid.uuid4(), uuid.uuid4()
    _, rows_s = await old_conversation(raw_db, a, a.s1, last_message_at=days_ago(1))
    _, rows_t = await old_conversation(raw_db, a, a.t1, last_message_at=days_ago(1))
    _, rows_b = await old_conversation(raw_db, b, b.s1, last_message_at=days_ago(1))
    c_student = await cite(raw_db, a, a.s1, rows_s[1], doc)
    c_teacher = await cite(raw_db, a, a.t1, rows_t[1], doc)
    c_other_doc = await cite(raw_db, a, a.s1, rows_s[1], other_doc, label="S2", ordinal=2)
    c_tenant_b = await cite(raw_db, b, b.s1, rows_b[1], doc)  # same document id, another tenant: not in b's queue
    await _enqueue(raw_db, a.id, doc)
    before_other = {k: getattr(c_student, k) for k in ("label", "ordinal", "document_title", "page_start", "page_end", "flagged", "similarity", "document_id", "user_id", "message_id")}

    caplog.set_level(logging.DEBUG)
    report = await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, a, b)

    for row in (c_student, c_teacher, c_other_doc, c_tenant_b):
        await raw_db.refresh(row)
    assert [(c.snippet, c.heading_path) for c in (c_student, c_teacher)] == [("", ""), ("", "")]
    assert (c_other_doc.snippet, c_other_doc.heading_path) == (f"{CANARY} snippet", f"{CANARY} heading")
    assert (c_tenant_b.snippet, c_tenant_b.heading_path) == (f"{CANARY} snippet", f"{CANARY} heading")
    assert {k: getattr(c_student, k) for k in before_other} == before_other, "only snippet and heading_path change"
    assert await _queue(raw_db, a.id) == []
    assert (report.blanked_documents, report.blanked_citations) == (1, 2)
    (row,) = await audit_rows(raw_db, a.id, "chat:citation_blank")
    assert row == (None, "document", str(doc), {"citations": 2})
    assert await audit_rows(raw_db, b.id, "chat:citation_blank") == []
    assert CANARY not in caplog.text


async def test_a_run_that_fails_before_the_commit_leaves_the_queue_row_and_the_next_run_repeats_it(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    doc = uuid.uuid4()
    _, rows = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(1))
    citation = await cite(raw_db, w, w.s1, rows[1], doc)
    await _enqueue(raw_db, w.id, doc)
    maint = make_maintenance(maint_engine, clock)

    real = ChatMaintenance._delete_queue_rows

    async def boom(self: ChatMaintenance, *a: Any, **k: Any) -> None:
        raise RuntimeError("crash before the commit")

    monkeypatch.setattr(ChatMaintenance, "_delete_queue_rows", boom)
    failed = await sweep(worker_dbs, tmp_path, maint, clock, w)  # the tenant's failure is logged, the run goes on
    await raw_db.refresh(citation)
    assert citation.snippet == f"{CANARY} snippet", "the whole transaction rolled back, the blanking too"
    assert await _queue(raw_db, w.id) == [doc]
    assert failed.blanked_documents == 0 and await audit_rows(raw_db, w.id, "chat:citation_blank") == []

    monkeypatch.setattr(ChatMaintenance, "_delete_queue_rows", real)
    ok = await sweep(worker_dbs, tmp_path, maint, clock, w)
    await raw_db.refresh(citation)
    assert (citation.snippet, citation.heading_path) == ("", "") and await _queue(raw_db, w.id) == []
    assert ok.blanked_documents == 1
    assert len(await audit_rows(raw_db, w.id, "chat:citation_blank")) == 1


async def test_blanking_is_idempotent_and_a_document_with_no_citations_still_clears_its_queue_row(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    w = academic_world.a
    doc = uuid.uuid4()
    await _enqueue(raw_db, w.id, doc)
    await _enqueue(raw_db, w.id, doc)  # the route and the sweeper both purged it: two rows for one document
    maint = make_maintenance(maint_engine, clock)
    report = await sweep(worker_dbs, tmp_path, maint, clock, w)
    assert await _queue(raw_db, w.id) == []
    assert report.blanked_citations == 0
    assert len(await audit_rows(raw_db, w.id, "chat:citation_blank")) == 1, "one audit row per document, not per queue row"


async def test_the_queue_is_drained_in_batches_and_one_tenants_queue_never_touches_anothers(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    a, b = academic_world.a, academic_world.b
    docs = [uuid.uuid4() for _ in range(5)]
    for d in docs:
        await _enqueue(raw_db, a.id, d)
    await _enqueue(raw_db, b.id, uuid.uuid4())
    small = make_maintenance(maint_engine, clock, batch_size=2, max_batches=1)
    await sweep(worker_dbs, tmp_path, small, clock, a)
    assert await _queue(raw_db, a.id) == docs[2:], "oldest first, two per run"
    assert len(await _queue(raw_db, b.id)) == 1, "tenant b was not swept"


# ── the purge route and the tombstone purge insert the queue row ──────────────


async def _soft_deleted(raw_db: AsyncSession, w: Any, *, deleted_days_ago: float) -> Any:
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="archived", error_code=None)
    await raw_db.execute(text("UPDATE app.documents SET deleted_at = now() - make_interval(days => :d) WHERE id = :i"),
                         {"d": deleted_days_ago, "i": made.document.id})
    await raw_db.commit()
    return made


async def test_the_purge_route_inserts_exactly_one_queue_row_in_the_purge_transaction(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path
) -> None:
    w = academic_world.a
    made = await _soft_deleted(raw_db, w, deleted_days_ago=1)
    resp = await client.post(f"/api/v1/admin/documents/{made.document.id}/purge", headers=auth(w, w.admin))
    assert resp.status_code == 204, resp.text
    assert await _queue(raw_db, w.id) == [made.document.id]


async def test_a_purge_that_rolls_back_leaves_no_queue_row_and_a_refused_purge_adds_none(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.v1 import documents

    w = academic_world.a
    live = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="published")
    refused = await client.post(f"/api/v1/admin/documents/{live.document.id}/purge", headers=auth(w, w.admin))
    assert refused.status_code == 409  # not soft-deleted
    assert await _queue(raw_db, w.id) == []

    made = await _soft_deleted(raw_db, w, deleted_days_ago=1)

    async def failing_audit(**_kw: Any) -> None:
        raise RuntimeError("the purge transaction fails after the delete")

    monkeypatch.setattr(documents, "record_audit_event", failing_audit)
    try:
        resp = await client.post(f"/api/v1/admin/documents/{made.document.id}/purge", headers=auth(w, w.admin))
    except RuntimeError:
        pass
    else:
        assert resp.status_code >= 500
    assert await _queue(raw_db, w.id) == [], "the purge rolled back, so did its queue row"
    assert (await raw_db.execute(text("SELECT count(*) FROM app.documents WHERE id = :i"), {"i": made.document.id})).scalar_one() == 1


async def test_the_api_purges_with_the_maintenance_url_unset(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, document_store_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review 6-0c: the API process never needs CHAT_MAINT_DATABASE_URL; without the worker the queue only grows."""
    monkeypatch.setattr(settings, "CHAT_MAINT_DATABASE_URL", SecretStr(""))
    w = academic_world.a
    made = await _soft_deleted(raw_db, w, deleted_days_ago=1)
    resp = await client.post(f"/api/v1/admin/documents/{made.document.id}/purge", headers=auth(w, w.admin))
    assert resp.status_code == 204
    assert await _queue(raw_db, w.id) == [made.document.id]


async def test_the_sweepers_tombstone_purge_queues_one_row_per_purged_document_and_blanks_in_the_same_run(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None,
) -> None:
    w = academic_world.a
    # The fake clock is the sweeper's "now": tombstones are measured against it (31 and 29 days before NOW)
    old1 = await _soft_deleted(raw_db, w, deleted_days_ago=0)
    old2 = await _soft_deleted(raw_db, w, deleted_days_ago=0)
    young = await _soft_deleted(raw_db, w, deleted_days_ago=0)
    for made, age in ((old1, 31), (old2, 40), (young, 29)):
        await raw_db.execute(text("UPDATE app.documents SET deleted_at = :at WHERE id = :i"), {"at": days_ago(age), "i": made.document.id})
    await raw_db.commit()
    _, rows = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(1))
    cited_old = await cite(raw_db, w, w.s1, rows[1], old1.document.id)
    cited_young = await cite(raw_db, w, w.s1, rows[1], young.document.id, label="S2", ordinal=2)

    without = await sweep(worker_dbs, tmp_path, None, clock, w)  # a worker with no maintenance object
    assert without.tombstones_purged == 2
    assert sorted(await _queue(raw_db, w.id)) == sorted([old1.document.id, old2.document.id]), "one row per purged document, none for the young one"
    await raw_db.refresh(cited_old)
    assert cited_old.snippet != "", "nothing is blanked without the maintenance object; the queue only grows"

    await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, w)
    await raw_db.refresh(cited_old)
    await raw_db.refresh(cited_young)
    assert cited_old.snippet == "" and cited_young.snippet != ""
    assert await _queue(raw_db, w.id) == []


# ── isolation between tenants and a sweeper without maintenance ───────────────


async def test_a_sweeper_without_the_maintenance_object_leaves_chat_alone(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, clock: FakeClock, tmp_path: Path,
    quiet_queue: None,
) -> None:
    w = academic_world.a
    await set_retention(raw_db, w, 30)
    conv, _ = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(400))
    stale = await pending_answer(raw_db, w, w.s1, conv, created_at=NOW - timedelta(hours=3))
    report = await sweep(worker_dbs, tmp_path, None, clock, w)
    await raw_db.refresh(stale)
    assert stale.outcome == "pending" and await count(raw_db, "conversations", w.id) == 1
    assert (report.conversations_deleted, report.stale_marked, report.blanked_documents) == (0, 0, 0)
    assert await audit_rows(raw_db, w.id, "chat:retention_purge") == []


async def test_one_tenants_maintenance_failure_does_not_stop_the_next_tenant(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = academic_world.a, academic_world.b
    answers = {}
    for w in (a, b):
        conv, _ = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(1))
        answers[w.id] = await pending_answer(raw_db, w, w.s1, conv, created_at=NOW - timedelta(hours=1))
    real = ChatMaintenance.mark_stale_pending

    async def flaky(self: ChatMaintenance, ref: TenantRef) -> int:
        if ref.tenant_id == a.id:
            raise RuntimeError("tenant a is broken")
        return await real(self, ref)

    monkeypatch.setattr(ChatMaintenance, "mark_stale_pending", flaky)
    report = await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, a, b)
    for row in answers.values():
        await raw_db.refresh(row)
    assert answers[a.id].outcome == "pending" and answers[b.id].outcome == "interrupted"
    assert report.stale_marked == 1


async def test_the_sweep_logs_ids_and_counts_only_never_chat_text(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, maint_engine: AsyncEngine,
    clock: FakeClock, tmp_path: Path, quiet_queue: None, caplog: pytest.LogCaptureFixture,
) -> None:
    w = academic_world.a
    await set_retention(raw_db, w, 30)
    conv, rows = await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(45))
    await cite(raw_db, w, w.s1, rows[1], uuid.uuid4())
    await pending_answer(raw_db, w, w.s1, (await old_conversation(raw_db, w, w.s1, last_message_at=days_ago(1)))[0], created_at=NOW - timedelta(hours=1))
    caplog.set_level(logging.DEBUG)
    await sweep(worker_dbs, tmp_path, make_maintenance(maint_engine, clock), clock, w)
    assert CANARY not in caplog.text
    assert chat_maintenance.MAINT_ROLE == "app_chat_maint"
