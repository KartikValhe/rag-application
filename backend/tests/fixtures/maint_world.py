"""
tests/fixtures/maint_world.py
─────────────────────────────
Shared setup for the chat maintenance tests (step 6a-12b): the maintenance engine, a sweeper that holds it, and chat
rows written straight into the tables through the superuser session so a test controls every timestamp.

The maintenance engine is built from the superuser URL and runs `SET ROLE app_chat_maint` for every connection (owner
answer 1 of the 6a-12a brief: the role has no password in the repo). Every maintenance transaction then checks
current_user itself, so a test that builds the engine WITHOUT the role sees the refusal.

Time: one FakeClock. The sweeper and the maintenance object get the same clock; row timestamps are written explicitly
relative to it, so no test sleeps and none depends on the machine's date.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.db.models.chat import Conversation, Message, MessageCitation
from app.db.models.user import User
from app.services import chat_maintenance, document_store
from app.workers import sweeper
from app.workers.db import WorkerDatabases
from tests.fixtures.academic_world import WorldTenant
from tests.fixtures.worker import FakeClock, make_queue

CLOCK_START = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
CANARY = "CANARY-chat-text-7c1e"  # chat text that must never reach an audit row or a log line

SWEEP_CONFIG = sweeper.SweeperSettings(
    interval_seconds=60,
    missing_job_after_seconds=120,
    tombstone_retention_days=30,
    finished_job_retention_days=7,
    outbox_batch=50,
)


def make_maint_engine(role: str | None = "app_chat_maint") -> AsyncEngine:
    """A test-database engine from the superuser URL that runs as `role` (None: as the superuser itself)."""
    url = os.environ["MIGRATION_DATABASE_URL"]
    assert (make_url(url).database or "").endswith("_test"), "maintenance tests must use the test database"
    connect_args: dict[str, object] = {"statement_cache_size": 0}
    if role is not None:
        connect_args["server_settings"] = {"role": role}
    return create_async_engine(url, connect_args=connect_args)


def make_maintenance(
    engine: AsyncEngine, clock: FakeClock, *, batch_size: int = 500, max_batches: int = 50, stale_seconds: int = 120,
    usage_retention_days: int = 400,
) -> chat_maintenance.ChatMaintenance:
    return chat_maintenance.ChatMaintenance(
        engine, stale_seconds=stale_seconds, usage_retention_days=usage_retention_days, batch_size=batch_size,
        max_batches=max_batches, clock=clock,
    )


def make_sweeper_with(
    dbs: WorkerDatabases, tmp_path: Path, maintenance: chat_maintenance.ChatMaintenance | None, clock: FakeClock,
    only_tenants: Collection[uuid.UUID],
) -> sweeper.Sweeper:
    return sweeper.Sweeper(
        queue=make_queue(dbs, clock=clock),
        tenant_sessions=dbs.tenant_sessions,
        store=document_store.LocalPrivateStore(tmp_path),
        config=SWEEP_CONFIG,
        clock=clock,
        only_tenants=only_tenants,
        maintenance=maintenance,
    )


# ── Rows ──────────────────────────────────────────────────────────────────────


async def set_retention(raw_db: AsyncSession, w: WorldTenant, days: int | None) -> None:
    """The tenant's settings row with exactly this retention (None = NULL = keep everything)."""
    await raw_db.execute(
        text(
            "INSERT INTO app.tenant_settings (tenant_id, chat_retention_days) VALUES (:t, :d) "
            "ON CONFLICT (tenant_id) DO UPDATE SET chat_retention_days = EXCLUDED.chat_retention_days"
        ),
        {"t": w.id, "d": days},
    )
    await raw_db.commit()


async def old_conversation(
    raw_db: AsyncSession, w: WorldTenant, user: User, *, last_message_at: datetime, exchanges: int = 1,
    class_name: str = "10-A", subject_name: str | None = "Math", text_prefix: str = CANARY,
) -> tuple[Conversation, list[Message]]:
    """A conversation whose last message is at `last_message_at`, with `exchanges` question/answer pairs (2 rows each)."""
    conv = Conversation(
        tenant_id=w.id, user_id=user.id, class_id=w.classes[class_name].id,
        subject_id=w.subjects[subject_name].id if subject_name else None,
        created_at=last_message_at - timedelta(minutes=5), last_message_at=last_message_at,
    )
    raw_db.add(conv)
    await raw_db.flush()
    rows: list[Message] = []
    for i in range(exchanges):
        at = last_message_at - timedelta(minutes=exchanges - i)
        q = Message(tenant_id=w.id, conversation_id=conv.id, user_id=user.id, role="user",
                    content=f"{text_prefix} question {i}", client_request_id=uuid.uuid4(), created_at=at)
        raw_db.add(q)
        await raw_db.flush()
        a = Message(tenant_id=w.id, conversation_id=conv.id, user_id=user.id, role="assistant",
                    content=f"{text_prefix} answer {i}", reply_to_id=q.id, outcome="answered", route="rag",
                    created_at=at + timedelta(seconds=1))
        raw_db.add(a)
        await raw_db.flush()
        rows += [q, a]
    conv.message_count = len(rows)
    await raw_db.commit()
    await raw_db.refresh(conv)
    return conv, rows


async def pending_answer(
    raw_db: AsyncSession, w: WorldTenant, user: User, conv: Conversation, *, created_at: datetime,
) -> Message:
    """An assistant row still `pending` (and the question it answers), created at `created_at`."""
    q = Message(tenant_id=w.id, conversation_id=conv.id, user_id=user.id, role="user", content=f"{CANARY} q",
                client_request_id=uuid.uuid4(), created_at=created_at - timedelta(seconds=1))
    raw_db.add(q)
    await raw_db.flush()
    a = Message(tenant_id=w.id, conversation_id=conv.id, user_id=user.id, role="assistant", content="",
                reply_to_id=q.id, outcome="pending", created_at=created_at)
    raw_db.add(a)
    await raw_db.commit()
    await raw_db.refresh(a)
    return a


async def cite(
    raw_db: AsyncSession, w: WorldTenant, user: User, message: Message, document_id: uuid.UUID, *, label: str = "S1",
    ordinal: int = 1, snippet: str = f"{CANARY} snippet", heading: str | None = f"{CANARY} heading",
) -> MessageCitation:
    row = MessageCitation(
        tenant_id=w.id, message_id=message.id, user_id=user.id, label=label, ordinal=ordinal, chunk_id=uuid.uuid4(),
        document_id=document_id, version_id=uuid.uuid4(), document_title="Intro to Physics", version_no=1,
        file_kind="pdf", location_kind="page", page_start=3, page_end=4, heading_path=heading, snippet=snippet,
        flagged=True, similarity=0.5,
    )
    raw_db.add(row)
    await raw_db.commit()
    await raw_db.refresh(row)
    return row


async def usage_event(raw_db: AsyncSession, w: WorldTenant, *, created_at: datetime) -> uuid.UUID:
    event_id = uuid.uuid4()
    await raw_db.execute(
        text(
            "INSERT INTO app.llm_usage_events (tenant_id, id, purpose, provider, model_key, prompt_tokens, "
            "completion_tokens, estimated, latency_ms, outcome, attempt, created_at) VALUES (:t, :id, 'answer', 'groq', "
            "'groq:openai/gpt-oss-120b', 10, 5, false, 120, 'ok', 1, :at)"
        ),
        {"t": w.id, "id": event_id, "at": created_at},
    )
    await raw_db.commit()
    return event_id


async def count(raw_db: AsyncSession, table: str, tenant_id: uuid.UUID) -> int:
    await raw_db.commit()  # end the snapshot: see what other connections committed
    return int((await raw_db.execute(text(f"SELECT count(*) FROM app.{table} WHERE tenant_id = :t"), {"t": tenant_id})).scalar_one())


async def audit_rows(raw_db: AsyncSession, tenant_id: uuid.UUID, action: str) -> list[tuple[uuid.UUID | None, str, str | None, dict[str, object]]]:
    """(actor_id, target_type, target_id, details) of the tenant's audit rows of one action, oldest first."""
    await raw_db.commit()
    rows = (await raw_db.execute(
        text("SELECT actor_id, target_type, target_id, details FROM app.audit_log WHERE tenant_id = :t AND action = :a ORDER BY created_at, id"),
        {"t": tenant_id, "a": action},
    )).all()
    return [(r[0], r[1], r[2], r[3]) for r in rows]
