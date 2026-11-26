"""
app/services/chat_messages.py
─────────────────────────────
One chat turn of POST /api/v1/chat/conversations/{id}/messages (docs/phase6-plan.md 2.1, 8.1-8.6, step 6a-9b).

prepare_turn() does every check before the stream, in the plan's order (8.1), and inserts the rows (T1):
  permission (the route's require_released, 403) -> chat_disabled (503) -> the caller's own conversation (404) ->
  the read scope REBUILT for the conversation's class and subject on every message (404, owner decision 1) ->
  text checks after cleaning (422) -> idempotency (409, with the existing assistant row) -> the four limits of 7.7 in
  ONE atomic acquire (429 / 503) -> T1: the question and the 'pending' assistant row in one transaction.
  Nothing before the limits uses a window (C15). The checks run in a first short transaction, the limits are taken
  with NO connection checked out, and T1 is a second short transaction (the 6a-9a pattern). A scope change between
  the two applies from the next message (accepted gap, plan 4.2).

ChatTurn.events() is the stream (8.3): meta -> the route's answer -> T3 (the pending row gets its final outcome) ->
done, or meta -> error. Every event is sent with no connection checked out (each transaction is closed first).
ChatTurn.close() always runs when the response ends, shielded and capped (8.6): a turn that did not reach its final
outcome is closed as 'cancelled' (client gone, partial, the text the client got) or 'error'; if even that fails the
row stays 'pending' and the sweeper (6a-12) marks it 'interrupted'. The stream slots are released in every case.

Step 6a-9b answers schedule and clarify questions (app/services/chat_schedule.py). A "rag" question gets the fixed
answer_unavailable text: the RAG answer (embedding, retrieval, prompt, LLM) is step 6a-10 (CHAT_RAG_ENABLED stays off).

Logs carry ids, codes, numbers and error class names only: never the question, the answer or a title (plan 9.5).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import anyio
import structlog
from fastapi import HTTPException, status
from sqlalchemy import and_, case, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core import slots
from app.core.config import settings
from app.core.tenant_db import chat_session_context
from app.db.models.academic import Class, Subject
from app.db.models.chat import (
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    OUTCOME_PENDING,
    Conversation,
    Message,
)
from app.db.models.user import User
from app.services import (
    chat_conversations,
    chat_language,
    chat_router,
    chat_schedule,
    chat_sse,
    document_scope,
    retrieval,
    schedule,
    tenant_settings,
)
from app.services.chat_router import NamedRef, RouteDecision, RouterContext
from app.services.document_text import clean_text

logger = structlog.get_logger(__name__)

RATE_KEY_PREFIX: Final = "chat:rate:user:"
DAY_KEY_PREFIX: Final = "chat:day:user:"
ACTIVE_USER_KEY_PREFIX: Final = "chat:active:user:"
ACTIVE_TENANT_KEY_PREFIX: Final = "chat:active:tenant:"
MINUTE_SECONDS: Final = 60
DAY_SECONDS: Final = 86_400
TITLE_CHARS: Final = 60
# The shielded closing step of a turn (plan 8.6): at most this long, then the row is left to the sweeper.
CLOSE_SECONDS: Final = 5.0

# One fixed text per code (plan 8.4): never a name, an id, SQL, a model message or the user's text.
ERROR_MESSAGES: Final[dict[str, str]] = {
    "invalid_message": "Please type a question.",
    "message_too_long": "Your question is too long. Please make it shorter.",
    "duplicate_request": "This question was already sent. Reload the conversation to see its answer.",
    "chat_rate_limit_exceeded": "You are sending questions too quickly. Please wait a moment and try again.",
    "chat_daily_limit_reached": "You have reached the limit of questions for today. Please try again later.",
    "chat_stream_in_progress": "An answer is still being written. Please wait until it is finished.",
    "chat_busy": "The study assistant is busy right now. Please try again in a moment.",
    "internal_error": "Something went wrong. Please try again later.",
}
# In the order of the limits given to the one atomic acquire.
LIMIT_CODES: Final = ("chat_rate_limit_exceeded", "chat_daily_limit_reached", "chat_stream_in_progress", "chat_busy")

# The `error` event texts, in the template language of the question. The Marathi ones need the native review of plan
# 4.4 before real students see them (pilot gate, 6a-13).
STREAM_ERROR_MESSAGES: Final[dict[str, dict[str, str]]] = {
    "en": {
        "schedule_unavailable": "The timetable cannot be read right now. Please try again in a few minutes.",
        "answer_unavailable": "The assistant is not available right now. Please try again later.",
        "stream_interrupted": "The answer was interrupted. Please ask again.",
        "internal_error": "Something went wrong. Please try again later.",
    },
    "mr": {
        "schedule_unavailable": "वेळापत्रक आत्ता वाचता येत नाही. कृपया थोड्या वेळाने पुन्हा प्रयत्न करा.",
        "answer_unavailable": "सहाय्यक आत्ता उपलब्ध नाही. कृपया नंतर पुन्हा प्रयत्न करा.",
        "stream_interrupted": "उत्तर मध्येच थांबले. कृपया पुन्हा विचारा.",
        "internal_error": "काहीतरी चुकले. कृपया नंतर पुन्हा प्रयत्न करा.",
    },
}


def clock() -> datetime:
    """The turn's 'now' (tests replace it). Only the schedule answer uses it; row times come from the database."""
    return datetime.now(UTC)


def _slots() -> slots.SlotManager:
    return slots.default_manager


def _error(status_code: int, code: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": ERROR_MESSAGES[code], **extra})


def clean_message(raw: str) -> str:
    """The question as stored and routed: cleaned like a retrieval query (the ingest clean_text rules), then 1 to
    CHAT_MAX_INPUT_CHARS characters with at least one letter or digit. 422 otherwise, with the fixed text."""
    cleaned = clean_text(raw)
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_message")
    if len(cleaned) > settings.CHAT_MAX_INPUT_CHARS:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "message_too_long")
    return cleaned


# ── Before the stream ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PreparedTurn:
    user: User  # detached, fully loaded (require_released)
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    message_id: uuid.UUID  # the pending assistant row: the id of the `meta` event
    question: str
    language: str  # detected (stored on the row)
    template_language: str  # the templates and error texts used
    router_ctx: RouterContext
    now: datetime
    lease: slots.SlotLease
    started: float  # time.monotonic() when the request reached the route

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.user.tenant_id

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id


@dataclass(frozen=True)
class _Existing:
    message_id: uuid.UUID
    outcome: str


async def _own_conversation(db: AsyncSession, user: User, conversation_id: uuid.UUID) -> Any:
    return (await db.execute(
        select(Conversation.class_id, Conversation.subject_id).where(
            Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.id, Conversation.id == conversation_id)
    )).one_or_none()


async def _existing_answer(
    db: AsyncSession, user: User, conversation_id: uuid.UUID, client_request_id: uuid.UUID
) -> _Existing | None:
    """The assistant row of an earlier question with this client_request_id. A row still 'pending' after
    CHAT_PENDING_STALE_SECONDS is reported as 'interrupted' (read time only, plan 2.1)."""
    question = aliased(Message)
    answer = aliased(Message)
    stale = func.now() - timedelta(seconds=settings.CHAT_PENDING_STALE_SECONDS)
    outcome = case(
        (and_(answer.outcome == OUTCOME_PENDING, answer.created_at < stale), "interrupted"), else_=answer.outcome
    )
    row = (await db.execute(
        select(answer.id, outcome)
        .select_from(question)
        .join(answer, and_(answer.tenant_id == question.tenant_id, answer.conversation_id == question.conversation_id,
                           answer.reply_to_id == question.id, answer.role == MESSAGE_ROLE_ASSISTANT))
        .where(question.tenant_id == user.tenant_id, question.user_id == user.id,
               question.conversation_id == conversation_id, question.role == MESSAGE_ROLE_USER,
               question.client_request_id == client_request_id)
    )).one_or_none()
    return None if row is None else _Existing(row[0], str(row[1]))


def _duplicate(existing: _Existing) -> HTTPException:
    return _error(status.HTTP_409_CONFLICT, "duplicate_request", message_id=str(existing.message_id),
                  outcome=existing.outcome)


async def _router_context(db: AsyncSession, user: User, pairs: frozenset[tuple[uuid.UUID, uuid.UUID]],
                          now: datetime) -> RouterContext:
    """The names the router may match: the classes the schedule service authorises (role rules of schedule.py) and
    the subjects of the caller's read pairs. Nothing outside them can ever be matched (plan 3.3)."""
    roles = await schedule.get_user_role_names(db, user.tenant_id, user.id)
    role: Any = "admin" if user.is_admin or "tenant_admin" in roles else "teacher" if "teacher" in roles else "student"
    class_ids = await schedule.resolve_authorized_class_ids(db, user)
    subject_ids = {subject_id for _, subject_id in pairs}
    classes = (await db.execute(
        select(Class.id, Class.name).where(Class.tenant_id == user.tenant_id, Class.id.in_(class_ids))
    )).all() if class_ids else []
    subjects = (await db.execute(
        select(Subject.id, Subject.name).where(Subject.tenant_id == user.tenant_id, Subject.id.in_(subject_ids))
    )).all() if subject_ids else []
    timezone = schedule.get_tenant_zoneinfo(await schedule.get_tenant_timezone(db, user.tenant_id))
    return RouterContext(
        role=role,
        classes=tuple(NamedRef(r[0], r[1]) for r in sorted(classes, key=lambda r: (r[1], str(r[0])))),
        subjects=tuple(NamedRef(r[0], r[1]) for r in sorted(subjects, key=lambda r: (r[1], str(r[0])))),
        timezone=timezone,
        now=now,
    )


def _limits(user: User) -> list[slots.SlotLimit]:
    return [
        slots.SlotLimit(f"{RATE_KEY_PREFIX}{user.id}", settings.CHAT_MESSAGES_PER_USER_PER_MINUTE, MINUTE_SECONDS),
        slots.SlotLimit(f"{DAY_KEY_PREFIX}{user.id}", settings.CHAT_MESSAGES_PER_USER_PER_DAY, DAY_SECONDS),
        slots.SlotLimit(f"{ACTIVE_USER_KEY_PREFIX}{user.id}", settings.CHAT_STREAMS_PER_USER,
                        settings.CHAT_STREAM_SLOT_TTL_SECONDS),
        slots.SlotLimit(f"{ACTIVE_TENANT_KEY_PREFIX}{user.tenant_id}", settings.CHAT_STREAMS_PER_TENANT,
                        settings.CHAT_STREAM_SLOT_TTL_SECONDS),
    ]


async def _acquire(user: User) -> slots.SlotLease:
    """All four limits in one atomic step: a rejection consumes nothing on the others. Called with no connection
    checked out. 429 with Retry-After; 503 service_unavailable when Redis is down outside development."""
    try:
        return await _slots().acquire(_limits(user), unavailable_message=chat_conversations.UNAVAILABLE_MESSAGE)
    except slots.SlotRejected as rej:
        retry = max(1, rej.retry_after_seconds)
        code = LIMIT_CODES[rej.index]
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": code, "message": ERROR_MESSAGES[code], "retry_after_seconds": retry},
            headers={"Retry-After": str(retry)},
        ) from None


def stream_lease(lease: slots.SlotLease) -> slots.SlotLease:
    """The part of a turn's lease that is given back when the stream ends: the two concurrency slots. The minute and
    day windows are kept (they count questions, not streams)."""
    return slots.SlotLease(token=lease.token, keys=lease.keys[2:], started_at=lease.started_at, backend=lease.backend)


async def _release(lease: slots.SlotLease) -> None:
    with anyio.CancelScope(shield=True):
        await _slots().release(lease)


async def _bump_conversation(db: AsyncSession, user: User, conversation_id: uuid.UUID, title: str) -> bool:
    """Two more rows, new activity, and the title if it has none yet. Also locks the conversation row, so two turns
    of one conversation insert one after the other. False when the conversation is gone."""
    res = await db.execute(
        update(Conversation)
        .where(Conversation.tenant_id == user.tenant_id, Conversation.user_id == user.id, Conversation.id == conversation_id)
        .values(message_count=Conversation.message_count + 2, last_message_at=func.now(),
                title=func.coalesce(Conversation.title, title))
        .returning(Conversation.id)
        .execution_options(synchronize_session=False)
    )
    return res.scalar_one_or_none() is not None


async def _insert_user_row(db: AsyncSession, user: User, conversation_id: uuid.UUID, question: str,
                           client_request_id: uuid.UUID) -> uuid.UUID:
    res = await db.execute(insert(Message).values(
        tenant_id=user.tenant_id, conversation_id=conversation_id, user_id=user.id, role=MESSAGE_ROLE_USER,
        content=question, client_request_id=client_request_id, created_at=func.clock_timestamp(),
    ).returning(Message.id))
    return uuid.UUID(str(res.scalar_one()))


async def _insert_assistant_row(db: AsyncSession, user: User, conversation_id: uuid.UUID,
                                user_message_id: uuid.UUID) -> uuid.UUID:
    """The answer row exists from T1 on (review 6-0b): outcome 'pending', empty content. clock_timestamp() puts it
    strictly after its question even inside the same transaction."""
    res = await db.execute(insert(Message).values(
        tenant_id=user.tenant_id, conversation_id=conversation_id, user_id=user.id, role=MESSAGE_ROLE_ASSISTANT,
        content="", reply_to_id=user_message_id, outcome=OUTCOME_PENDING, created_at=func.clock_timestamp(),
    ).returning(Message.id))
    return uuid.UUID(str(res.scalar_one()))


async def prepare_turn(user: User, conversation_id: uuid.UUID, content: str, client_request_id: uuid.UUID) -> PreparedTurn:
    """Every check before the stream and T1 (see the module docstring). Raises HTTPException for every refusal."""
    started = time.monotonic()
    now = clock()
    async with chat_session_context(user.tenant_id, user.id) as db:
        if not await tenant_settings.read_chat_enabled(db, user.tenant_id):
            raise chat_conversations.chat_error(status.HTTP_503_SERVICE_UNAVAILABLE, "chat_disabled")
        conv = await _own_conversation(db, user, conversation_id)
        if conv is None:
            raise document_scope.not_found()
        # The caller's CURRENT read scope, narrowed to this conversation (generic 404 when nothing is left).
        scope = await retrieval.build_retrieval_scope(db, user, purpose="read")
        scope.narrow(conv.class_id, conv.subject_id)
        question = clean_message(content)
        existing = await _existing_answer(db, user, conversation_id, client_request_id)
        if existing is not None:
            raise _duplicate(existing)
        ctx = await _router_context(db, user, scope.pairs, now)

    lease = await _acquire(user)
    try:
        async with chat_session_context(user.tenant_id, user.id) as db:
            if not await _bump_conversation(db, user, conversation_id, question[:TITLE_CHARS]):
                raise document_scope.not_found()
            user_message_id = await _insert_user_row(db, user, conversation_id, question, client_request_id)
            message_id = await _insert_assistant_row(db, user, conversation_id, user_message_id)
    except IntegrityError:
        # The same question sent twice at once: the unique client_request_id lets one through. Give back every slot
        # this request took (it asked nothing) and point at the winner's answer.
        await _release(lease)
        async with chat_session_context(user.tenant_id, user.id) as db:
            existing = await _existing_answer(db, user, conversation_id, client_request_id)
        if existing is not None:
            raise _duplicate(existing) from None
        logger.error("chat_persist_error", stage="t1", tenant_id=str(user.tenant_id),
                     conversation_id=str(conversation_id), error_class="IntegrityError")
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error") from None
    except HTTPException:
        await _release(lease)
        raise
    except Exception as exc:
        await _release(lease)
        logger.error("chat_persist_error", stage="t1", tenant_id=str(user.tenant_id),
                     conversation_id=str(conversation_id), error_class=type(exc).__name__)
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error") from None
    except BaseException:
        await _release(lease)
        raise

    language = chat_language.detect(question, settings.CHAT_DEFAULT_LANGUAGE)
    return PreparedTurn(
        user=user, conversation_id=conversation_id, user_message_id=user_message_id, message_id=message_id,
        question=question, language=language, template_language=chat_schedule.template_language(language),
        router_ctx=ctx, now=now, lease=lease, started=started,
    )


# ── Writing the answer row ────────────────────────────────────────────────────


async def _update_pending(p: PreparedTurn, values: dict[str, Any]) -> bool:
    """Give the caller's own pending answer row its final state (the chat_tenant_update_pending policy allows nothing
    else). False when the row is no longer pending (already final): nothing is changed then."""
    async with chat_session_context(p.tenant_id, p.user_id) as db:
        res = await db.execute(
            update(Message)
            .where(Message.tenant_id == p.tenant_id, Message.user_id == p.user_id, Message.id == p.message_id,
                   Message.role == MESSAGE_ROLE_ASSISTANT, Message.outcome == OUTCOME_PENDING)
            .values(**values, completed_at=func.clock_timestamp())
            .returning(Message.id)
            .execution_options(synchronize_session=False)
        )
        updated = res.scalar_one_or_none() is not None
        if updated:
            await db.execute(
                update(Conversation)
                .where(Conversation.tenant_id == p.tenant_id, Conversation.user_id == p.user_id,
                       Conversation.id == p.conversation_id)
                .values(last_message_at=func.now())
                .execution_options(synchronize_session=False)
            )
        return updated


async def _finish_row(p: PreparedTurn, values: dict[str, Any]) -> bool:
    """T3: the turn's final outcome."""
    return await _update_pending(p, values)


async def _mark_unfinished(p: PreparedTurn, values: dict[str, Any]) -> bool:
    """The shielded closing step: a turn that ended without its final outcome (cancelled or failed)."""
    return await _update_pending(p, values)


# ── The stream ────────────────────────────────────────────────────────────────


def _ms(since: float) -> int:
    return max(0, int((time.monotonic() - since) * 1000))


@dataclass
class ChatTurn:
    p: PreparedTurn
    sent_text: str = ""  # what the client was sent (an event counts once the stream asked for the next one)
    finished: bool = False  # the row has its final outcome
    ended: bool = False  # the stream ran to its end (not cancelled)
    outcome: str = OUTCOME_PENDING
    partial: bool = False
    record: dict[str, Any] = field(default_factory=dict)  # router record for the row (no text)
    latency: dict[str, int] = field(default_factory=dict)
    _closed: bool = False

    def _ids(self) -> dict[str, str]:
        return {"tenant_id": str(self.p.tenant_id), "conversation_id": str(self.p.conversation_id),
                "message_id": str(self.p.message_id)}

    def _row_values(self, outcome: str, partial: bool) -> dict[str, Any]:
        self.latency["total"] = _ms(self.p.started)
        return {"content": self.sent_text, "outcome": outcome, "partial": partial, "latency_ms": dict(self.latency),
                **self.record}

    async def _persist(self, outcome: str, partial: bool) -> bool:
        try:
            done = await _finish_row(self.p, self._row_values(outcome, partial))
        except Exception as exc:
            logger.error("chat_persist_error", stage="t3", error_class=type(exc).__name__, **self._ids())
            return False
        if done:
            self.finished, self.outcome, self.partial = True, outcome, partial
        return done

    def _error_event(self, code: str, partial: bool) -> bytes:
        message = STREAM_ERROR_MESSAGES[self.p.template_language if self.p.template_language in STREAM_ERROR_MESSAGES
                                        else "en"][code]
        return chat_sse.event("error", {"code": code, "message": message, "partial": partial})

    async def _schedule_answer(self, decision: RouteDecision) -> chat_schedule.ScheduleAnswer:
        """T2: the schedule service in its own short transaction (never an LLM, plan 3.5)."""
        p = self.p
        async with chat_session_context(p.tenant_id, p.user_id) as db:
            return await chat_schedule.answer(db, p.user, decision, p.router_ctx, p.template_language, p.now)

    async def events(self) -> AsyncIterator[bytes]:
        p = self.p
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.CHAT_STREAM_MAX_SECONDS - (time.monotonic() - p.started)
        t_router = time.monotonic()
        decision, error_class = chat_router.route_with_error(p.question, p.router_ctx)
        self.latency["router"] = _ms(t_router)
        if error_class is not None:
            logger.warning("chat_router_error", error_class=error_class, router_version=chat_router.ROUTER_VERSION,
                           **self._ids())
        self.record = {"route": decision.route, "intent": decision.intent, "rule_ids": list(decision.rule_ids),
                       "router_confidence": decision.confidence, "language": p.language}
        yield chat_sse.event("meta", {"message_id": str(p.message_id), "user_message_id": str(p.user_message_id),
                                      "route": decision.route, "language": p.language})

        if decision.route == "rag":
            # Step 6a-10 builds the RAG answer behind CHAT_RAG_ENABLED. Until then: the fixed text, no model call.
            await self._persist("error", False)
            self.ended = True
            yield self._error_event("answer_unavailable", False)
            return

        if decision.route == "clarify":
            result = chat_schedule.clarify_answer(decision, p.router_ctx, p.template_language)
        else:
            box: list[chat_schedule.ScheduleAnswer] = []
            t_schedule = time.monotonic()
            try:
                async with chat_sse.heartbeat_while(self._schedule_answer(decision), box,
                                                    interval=settings.CHAT_STREAM_HEARTBEAT_SECONDS,
                                                    deadline=deadline) as beats:
                    async for _ in beats:
                        yield chat_sse.PING
            except chat_sse.StreamDeadline:
                await self._persist("interrupted", True)
                self.ended = True
                yield self._error_event("stream_interrupted", True)
                return
            except Exception as exc:
                logger.error("chat_schedule_error", error_class=type(exc).__name__, **self._ids())
                await self._persist("error", False)
                self.ended = True
                yield self._error_event("schedule_unavailable", False)
                return
            self.latency["schedule"] = _ms(t_schedule)
            result = box[0]

        self.latency["first_token"] = _ms(p.started)
        yield chat_sse.event("token", {"text": result.text})
        self.sent_text = result.text
        if not await self._persist(result.outcome, False):
            self.ended = True
            yield self._error_event("internal_error", False)
            return
        self.ended = True
        yield chat_sse.event("done", {
            "outcome": result.outcome, "partial": False,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False},
            "latency_ms": {"first_token": self.latency["first_token"], "total": self.latency["total"]},
        })

    async def close(self, stream: Any) -> None:
        """Runs when the response ends, whatever happened (shielded, at most CLOSE_SECONDS for the database part):
        finish the generator, close an unfinished row, release the stream slots, log the turn."""
        if self._closed:
            return
        self._closed = True
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(CLOSE_SECONDS):
                try:
                    await stream.aclose()
                except Exception as exc:
                    logger.error("chat_stream_close_error", error_class=type(exc).__name__, **self._ids())
                if not self.finished:
                    outcome = "error" if self.ended else "cancelled"
                    try:
                        if await _mark_unfinished(self.p, self._row_values(outcome, outcome == "cancelled")):
                            self.finished, self.outcome, self.partial = True, outcome, outcome == "cancelled"
                    except Exception as exc:
                        # The row stays 'pending'; the sweeper marks it 'interrupted' (6a-12).
                        logger.error("chat_persist_error", stage="close", error_class=type(exc).__name__,
                                     **self._ids())
            await _slots().release(stream_lease(self.p.lease))
            logger.info("chat_turn", route=self.record.get("route"), intent=self.record.get("intent"),
                        rule_ids=self.record.get("rule_ids", []), outcome=self.outcome,
                        language=self.p.language, partial=self.partial, latency_ms=dict(self.latency),
                        router_version=chat_router.ROUTER_VERSION, **self._ids())
