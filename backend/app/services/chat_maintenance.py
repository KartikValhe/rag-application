"""
app/services/chat_maintenance.py
────────────────────────────────
The chat maintenance connection and what runs through it (docs/phase6-plan.md 9.2, 9.4; step 6a-12b). WORKER ONLY: the
API process never reads CHAT_MAINT_DATABASE_URL or imports this module (a source scan enforces it).

Everything here runs as the database role `app_chat_maint` (grants: SELECT and DELETE on the chat tables and the usage
events, two UPDATE columns on the citations, three on pending assistant rows; no INSERT; nothing on tenant_settings or
audit_log). Each call opens ONE transaction of its own for ONE tenant: it first checks `current_user = 'app_chat_maint'`
and refuses to run as anyone else (so neither the superuser nor app_tenant can be used by mistake), then sets
app.tenant_id transaction-locally. The tenant comes from a TenantRef (the sweeper's tenant list), never from a loose id.

  mark_stale_pending   assistant rows still `pending` after CHAT_PENDING_STALE_SECONDS become `interrupted`
  delete_expired       conversations whose last message is older than the tenant's retention (with messages, citations
                       and feedback by cascade) and usage events older than LLM_USAGE_RETENTION_DAYS, in batches
  count_retention_due  the same, counted, nothing deleted (the dry run)
  drain_blank_queue    blank the snippet and heading of the citations of every document in the purge queue, then delete
                       the queue rows, in ONE transaction (a crash before the commit repeats the idempotent blanking)

RETENTION SAFETY (owner decisions Q4 and C35): the retention period is passed in by the caller. `None` (keep everything)
deletes and counts NOTHING and opens no connection. Anything that is not a whole number from 30 to 3650 raises ValueError
before any connection is used, so no code path can delete with a made-up period. The value itself is read by the sweeper
through an app_tenant session (this role has no grant on tenant_settings).

Audit rows are not written here: the role has no grant on audit_log. The sweeper writes them in a separate app_tenant
session. Nothing here logs or returns chat text: ids, counts and codes only.

The test connection: the engine is built from the superuser URL with `SET ROLE app_chat_maint` on connect (the role has no
password in the repo, plan 8.8). Production is a plain engine on CHAT_MAINT_DATABASE_URL.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.db.models.tenant_settings import CHAT_RETENTION_DAYS_MAX, CHAT_RETENTION_DAYS_MIN
from app.workers.db import TenantRef

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

MAINT_ROLE: Final = "app_chat_maint"
RETENTION_DAYS_MIN: Final = CHAT_RETENTION_DAYS_MIN
RETENTION_DAYS_MAX: Final = CHAT_RETENTION_DAYS_MAX
DEFAULT_BATCH_SIZE: Final = 500  # plan 9.4: batches of 500
DEFAULT_MAX_BATCHES: Final = 50  # per kind, tenant and run: one big tenant cannot hold the sweeper for ever

# The sweeper's own clock pattern: a test passes a fake clock as :now, production passes None (the database's now()).
_NOW: Final = "COALESCE(CAST(:now AS timestamptz), now())"
_SET_TENANT: Final = "SELECT set_config('app.tenant_id', :tid, true)"

_EXPIRED_CONVERSATIONS: Final = f"""
    SELECT id FROM app.conversations
    WHERE tenant_id = :t AND last_message_at < {_NOW} - make_interval(days => :days)
"""

# One statement per batch: the ids are chosen once, their messages are counted from the same snapshot, then the
# conversations go (messages, citations and feedback follow by cascade, which Postgres runs as the table owner).
_DELETE_CONVERSATIONS_SQL: Final = f"""
WITH due AS ({_EXPIRED_CONVERSATIONS} ORDER BY last_message_at, id LIMIT :batch),
msgs AS (SELECT count(*) AS n FROM app.messages m WHERE m.tenant_id = :t AND m.conversation_id IN (SELECT id FROM due)),
gone AS (DELETE FROM app.conversations c USING due WHERE c.tenant_id = :t AND c.id = due.id RETURNING c.id)
SELECT (SELECT count(*) FROM gone), (SELECT n FROM msgs)
"""

_DELETE_USAGE_SQL: Final = f"""
WITH due AS (
    SELECT id FROM app.llm_usage_events
    WHERE tenant_id = :t AND created_at < {_NOW} - make_interval(days => :days)
    ORDER BY created_at, id LIMIT :batch
),
gone AS (DELETE FROM app.llm_usage_events e USING due WHERE e.tenant_id = :t AND e.id = due.id RETURNING e.id)
SELECT count(*) FROM gone
"""

_COUNT_CONVERSATIONS_SQL: Final = f"SELECT count(*) FROM ({_EXPIRED_CONVERSATIONS}) AS due"
_COUNT_MESSAGES_SQL: Final = f"""
SELECT count(*) FROM app.messages m WHERE m.tenant_id = :t AND m.conversation_id IN ({_EXPIRED_CONVERSATIONS})
"""
_COUNT_USAGE_SQL: Final = f"""
SELECT count(*) FROM app.llm_usage_events
WHERE tenant_id = :t AND created_at < {_NOW} - make_interval(days => :days)
"""

_STALE_SQL: Final = f"""
UPDATE app.messages SET outcome = 'interrupted', partial = true, completed_at = {_NOW}
WHERE tenant_id = :t AND role = 'assistant' AND outcome = 'pending'
  AND created_at < {_NOW} - make_interval(secs => :secs)
"""

_QUEUE_BATCH_SQL: Final = """
SELECT id, document_id FROM app.chat_citation_blank_queue
WHERE tenant_id = :t ORDER BY created_at, id LIMIT :batch
"""
# Unconditional on purpose: the blanking is idempotent and a repeat reports the same citation count.
_BLANK_SQL: Final = """
UPDATE app.message_citations SET snippet = '', heading_path = '' WHERE tenant_id = :t AND document_id = :d
"""


class MaintenanceNotConfigured(RuntimeError):
    """The maintenance connection is set up wrongly (the worker does not start). Never holds the URL or a password."""


class MaintenanceRoleError(RuntimeError):
    """A maintenance transaction found itself connected as another role and refused to run."""


class RetentionInterrupted(RuntimeError):
    """A retention run failed after some batches were already committed; `counts` is what really went."""

    def __init__(self, counts: RetentionCounts) -> None:
        super().__init__("retention_interrupted")
        self.counts = counts


@dataclass(frozen=True, slots=True)
class RetentionCounts:
    conversations: int = 0
    messages: int = 0
    usage_events: int = 0

    def __add__(self, other: RetentionCounts) -> RetentionCounts:
        return RetentionCounts(
            self.conversations + other.conversations, self.messages + other.messages,
            self.usage_events + other.usage_events,
        )

    @property
    def any(self) -> bool:
        return bool(self.conversations or self.messages or self.usage_events)


@dataclass(frozen=True, slots=True)
class BlankedDocument:
    document_id: uuid.UUID
    citations: int


def check_url_user(url: str) -> None:
    """Raise MaintenanceNotConfigured unless the user name in `url` is exactly app_chat_maint. The message never echoes
    the URL, the host or a password."""
    try:
        user = make_url(url).username
    except ArgumentError:
        raise MaintenanceNotConfigured("CHAT_MAINT_DATABASE_URL is not a valid database URL") from None
    if user != MAINT_ROLE:
        raise MaintenanceNotConfigured(
            f"CHAT_MAINT_DATABASE_URL must connect as the database role {MAINT_ROLE} (any other user name is refused)"
        )


def validate_retention_days(days: object) -> int:
    """A whole number from 30 to 3650 (a bool or a float is not one), else ValueError."""
    if type(days) is not int or not RETENTION_DAYS_MIN <= days <= RETENTION_DAYS_MAX:
        raise ValueError(f"retention days must be a whole number from {RETENTION_DAYS_MIN} to {RETENTION_DAYS_MAX}")
    return days


def create_maintenance_engine(url: str) -> AsyncEngine:
    """The production engine. statement_cache_size=0 like every engine under app/ (poolers); two connections are plenty:
    the sweeper runs one tenant at a time."""
    return create_async_engine(
        url, pool_size=2, max_overflow=1, pool_pre_ping=True,
        connect_args={"statement_cache_size": 0, "command_timeout": 60}, query_cache_size=0,
    )


def _uuid_array(name: str) -> Any:
    return bindparam(name, type_=ARRAY(PG_UUID(as_uuid=True)))


class ChatMaintenance:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        stale_seconds: int,
        usage_retention_days: int,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_batches: int = DEFAULT_MAX_BATCHES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for name, value in (
            ("stale_seconds", stale_seconds), ("usage_retention_days", usage_retention_days),
            ("batch_size", batch_size), ("max_batches", max_batches),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a whole number of at least 1")
        self.engine = engine
        self.stale_seconds = stale_seconds
        self.usage_retention_days = usage_retention_days
        self.batch_size = batch_size
        self.max_batches = max_batches
        self.clock = clock

    @classmethod
    def from_settings(cls, s: Settings, *, clock: Callable[[], datetime] | None = None) -> ChatMaintenance | None:
        """None when CHAT_MAINT_DATABASE_URL is unset (this worker does no chat maintenance). Raises
        MaintenanceNotConfigured when it names any user but app_chat_maint."""
        url = s.CHAT_MAINT_DATABASE_URL.get_secret_value().strip()
        if not url:
            return None
        check_url_user(url)
        return cls(
            create_maintenance_engine(url), stale_seconds=s.CHAT_PENDING_STALE_SECONDS,
            usage_retention_days=s.LLM_USAGE_RETENTION_DAYS, clock=clock,
        )

    def _now(self) -> datetime | None:
        return self.clock() if self.clock is not None else None

    async def verify_role(self) -> None:
        """Start-up check: the engine really connects as app_chat_maint."""
        async with self.engine.connect() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar_one()
        if who != MAINT_ROLE:
            raise MaintenanceRoleError(f"the maintenance connection runs as {who!r}, expected {MAINT_ROLE!r}")

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def _transaction(self, ref: TenantRef) -> AsyncIterator[AsyncConnection]:
        """ONE transaction for one tenant, as app_chat_maint only: the role check comes before anything else."""
        if not isinstance(ref, TenantRef):
            raise TypeError("chat maintenance needs a TenantRef from the tenant list, not a bare tenant id")
        async with self.engine.begin() as conn:
            who = (await conn.execute(text("SELECT current_user"))).scalar_one()
            if who != MAINT_ROLE:
                raise MaintenanceRoleError(f"a maintenance transaction ran as {who!r}, expected {MAINT_ROLE!r}")
            await conn.execute(text(_SET_TENANT), {"tid": str(ref.tenant_id)})
            yield conn

    # ── stale pending answers ───────────────────────────────────────────────

    async def mark_stale_pending(self, ref: TenantRef) -> int:
        """Assistant rows still `pending` after stale_seconds become `interrupted` (partial, completed now). Only those:
        the role's UPDATE policy and column grants refuse anything else. Returns how many."""
        async with self._transaction(ref) as conn:
            result = await conn.execute(
                text(_STALE_SQL), {"t": ref.tenant_id, "secs": float(self.stale_seconds), "now": self._now()}
            )
        return int(getattr(result, "rowcount", 0) or 0)

    # ── retention ───────────────────────────────────────────────────────────

    async def count_retention_due(self, ref: TenantRef, retention_days: int | None) -> RetentionCounts:
        """What delete_expired would delete now (all of it, not one batch). Deletes nothing. None counts nothing."""
        if retention_days is None:
            return RetentionCounts()
        days = validate_retention_days(retention_days)
        usage_days = self.usage_retention_days
        async with self._transaction(ref) as conn:
            now = self._now()
            base = {"t": ref.tenant_id, "days": days, "now": now}
            conversations = (await conn.execute(text(_COUNT_CONVERSATIONS_SQL), base)).scalar_one()
            messages = (await conn.execute(text(_COUNT_MESSAGES_SQL), base)).scalar_one()
            usage = (await conn.execute(text(_COUNT_USAGE_SQL), {"t": ref.tenant_id, "days": usage_days, "now": now})).scalar_one()
        return RetentionCounts(int(conversations), int(messages), int(usage))

    async def _delete_conversation_batch(self, ref: TenantRef, days: int) -> tuple[int, int]:
        async with self._transaction(ref) as conn:
            row = (await conn.execute(
                text(_DELETE_CONVERSATIONS_SQL),
                {"t": ref.tenant_id, "days": days, "batch": self.batch_size, "now": self._now()},
            )).one()
        return int(row[0]), int(row[1])

    async def _delete_usage_batch(self, ref: TenantRef) -> int:
        async with self._transaction(ref) as conn:
            deleted = (await conn.execute(
                text(_DELETE_USAGE_SQL),
                {"t": ref.tenant_id, "days": self.usage_retention_days, "batch": self.batch_size, "now": self._now()},
            )).scalar_one()
        return int(deleted)

    async def delete_expired(self, ref: TenantRef, retention_days: int | None) -> RetentionCounts:
        """Delete the tenant's conversations older than `retention_days` and its usage events older than
        LLM_USAGE_RETENTION_DAYS, in committed batches (at most max_batches of each per call; the rest waits for the next
        run). `None` does nothing at all. If a batch fails after earlier ones committed, RetentionInterrupted carries the
        counts that really went, so the caller can still audit them."""
        if retention_days is None:
            return RetentionCounts()
        days = validate_retention_days(retention_days)
        total = RetentionCounts()
        try:
            for _ in range(self.max_batches):
                conversations, messages = await self._delete_conversation_batch(ref, days)
                total += RetentionCounts(conversations=conversations, messages=messages)
                if conversations < self.batch_size:
                    break
            for _ in range(self.max_batches):
                usage = await self._delete_usage_batch(ref)
                total += RetentionCounts(usage_events=usage)
                if usage < self.batch_size:
                    break
        except Exception as exc:
            if total.any:
                raise RetentionInterrupted(total) from exc
            raise
        return total

    # ── the purge queue ─────────────────────────────────────────────────────

    async def _delete_queue_rows(self, conn: AsyncConnection, ref: TenantRef, ids: Sequence[uuid.UUID]) -> None:
        await conn.execute(
            text("DELETE FROM app.chat_citation_blank_queue WHERE tenant_id = :t AND id = ANY(:ids)").bindparams(
                _uuid_array("ids")
            ),
            {"t": ref.tenant_id, "ids": list(ids)},
        )

    async def drain_blank_queue(self, ref: TenantRef) -> list[BlankedDocument]:
        """Blank the citations of every queued document (all users of the tenant) and delete the queue rows, one
        transaction per batch: a failure before its commit leaves the rows and the next run repeats it. One entry per
        document, however many queue rows it had. Returns what was blanked (for the audit)."""
        blanked: dict[uuid.UUID, int] = {}
        for _ in range(self.max_batches):
            async with self._transaction(ref) as conn:
                rows = (await conn.execute(text(_QUEUE_BATCH_SQL), {"t": ref.tenant_id, "batch": self.batch_size})).all()
                if not rows:
                    break
                batch: dict[uuid.UUID, int] = {}
                for document_id in dict.fromkeys(row[1] for row in rows):
                    result = await conn.execute(text(_BLANK_SQL), {"t": ref.tenant_id, "d": document_id})
                    batch[document_id] = int(getattr(result, "rowcount", 0) or 0)
                await self._delete_queue_rows(conn, ref, [row[0] for row in rows])
            for document_id, citations in batch.items():  # only after the commit
                blanked[document_id] = max(blanked.get(document_id, 0), citations)
            if len(rows) < self.batch_size:
                break
        return [BlankedDocument(d, n) for d, n in blanked.items()]
