"""
app/api/v1/chat.py
──────────────────
The chat API (docs/phase6-plan.md section 8). Step 6a-9a: the conversation routes; 6a-9b: the message route (SSE);
feedback and history in 6a-12.

  POST   /api/v1/chat/conversations                      create (201; 404 out of scope; 429; 503 chat_disabled)
  GET    /api/v1/chat/conversations                      the caller's own, newest activity first (cursor paging)
  GET    /api/v1/chat/conversations/{conversation_id}    one of the caller's own (404 otherwise)
  DELETE /api/v1/chat/conversations/{conversation_id}    hard delete, cascades, audited with ids and counts (204)
  POST   /api/v1/chat/conversations/{conversation_id}/messages
                                                         one question; 200 text/event-stream (app/services/chat_messages.py)

The message route re-checks the caller's CURRENT read scope on every message (owner decision, 6a-9b); list, get and
delete do not (a student can always delete their own conversation).

Every route depends on require_released("chat:use"): the permission check runs in its own short transaction and
gives its connection back before the handler starts (B100). Each handler then opens its own short
chat_session_context transactions (app.tenant_id and app.user_id set), so no pooled connection is held while a
route waits on anything else (the Redis window of create). Nobody reads another user's conversation, not an admin,
not a teacher of the class (Q4): "not found" and "not yours" are the same generic 404.
Logs carry ids only, never a title or any message text.
"""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.core.authz import require_released
from app.core.tenant_db import chat_session_context
from app.db.models.user import User
from app.schemas.chat import (
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationResponse,
    MessageCreateRequest,
    NamedRef,
)
from app.services import chat_conversations, chat_messages, chat_sse, document_scope
from app.services.audit import record_audit_event
from app.services.chat_conversations import ConversationView

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

ChatUser = Annotated[User, Depends(require_released("chat:use"))]


def _response(view: ConversationView) -> ConversationResponse:
    subject = None if view.subject_id is None or view.subject_name is None else NamedRef(id=view.subject_id, name=view.subject_name)
    return ConversationResponse(
        id=view.id,
        class_=NamedRef(id=view.class_id, name=view.class_name),
        subject=subject,
        title=view.title,
        created_at=view.created_at,
        last_message_at=view.last_message_at,
        message_count=view.message_count,
    )


@router.post("/conversations", status_code=status.HTTP_201_CREATED, response_model=ConversationResponse)
async def create_conversation(body: ConversationCreateRequest, user: ChatUser) -> ConversationResponse:
    """
    Start a conversation about one class (and optionally one subject). Checks, in order: permission (403), chat
    switched off for the tenant (503 chat_disabled), the pair in the caller's read scope (generic 404), the hourly
    window (429 conversation_rate_limit_exceeded; 503 when Redis is down outside development). The scope is read
    in the first transaction and the row is written in the second one; the window is taken in between with no
    connection checked out. A scope change between the two applies from the next request (accepted, plan 4.2).
    """
    async with chat_session_context(user.tenant_id, user.id) as db:
        target = await chat_conversations.creation_target(db, user, body.class_id, body.subject_id)
    await chat_conversations.check_creation_rate(user.id)
    async with chat_session_context(user.tenant_id, user.id) as db:
        view = await chat_conversations.insert_conversation(db, user, target)
    logger.info("chat_conversation_created", tenant_id=str(user.tenant_id), conversation_id=str(view.id))
    return _response(view)


class ChatStreamResponse(StreamingResponse):
    """The SSE response of one turn. Whatever ends the stream (done, error, a client disconnect that cancels it, a
    crash), the turn's shielded closing step runs once the response is over (plan 8.6): it finishes the generator,
    closes a row that has no final outcome yet and releases the stream slots."""

    def __init__(self, turn: chat_messages.ChatTurn) -> None:
        self._turn = turn
        super().__init__(turn.events(), media_type=chat_sse.MEDIA_TYPE, headers=chat_sse.HEADERS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._turn.close(self.body_iterator)


@router.post(
    "/conversations/{conversation_id}/messages",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "The answer as server-sent events"}},
)
async def post_message(conversation_id: uuid.UUID, body: MessageCreateRequest, user: ChatUser) -> Response:
    """
    Ask one question in one of the caller's own conversations; the answer is an SSE stream (plan 8.3). Before the
    stream, in order: permission (403), chat switched off (503 chat_disabled), the conversation (404), the caller's
    current read scope for its class and subject (404), the text after cleaning (422 invalid_message /
    message_too_long), a repeated client_request_id (409 duplicate_request with the existing answer's id and
    outcome), the limits (429 with Retry-After; 503 service_unavailable). No pooled connection is held while the
    stream is sent or a limit is taken.
    """
    prepared = await chat_messages.prepare_turn(user, conversation_id, body.content, body.client_request_id)
    return ChatStreamResponse(chat_messages.ChatTurn(prepared))


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    user: ChatUser,
    class_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=chat_conversations.MAX_PAGE)] = chat_conversations.DEFAULT_PAGE,
) -> ConversationListResponse:
    """The caller's own conversations, newest activity first. `class_id` only filters the caller's own list.
    422 invalid_cursor for a cursor this API did not write."""
    try:
        async with chat_session_context(user.tenant_id, user.id) as db:
            views, next_cursor = await chat_conversations.list_own(
                db, user, class_id=class_id, cursor=cursor, limit=limit
            )
    except chat_conversations.CursorError:
        raise chat_conversations.chat_error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_cursor") from None
    return ConversationListResponse(items=[_response(v) for v in views], next_cursor=next_cursor)


@router.get("/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(conversation_id: uuid.UUID, user: ChatUser) -> ConversationResponse:
    """One of the caller's own conversations; the generic 404 for any other id."""
    async with chat_session_context(user.tenant_id, user.id) as db:
        view = await chat_conversations.get_own(db, user, conversation_id)
    if view is None:
        raise document_scope.not_found()
    return _response(view)


@router.delete(
    "/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_conversation(conversation_id: uuid.UUID, request: Request, user: ChatUser) -> Response:
    """Delete one of the caller's own conversations at once (hard delete; messages, citations and feedback go with
    it). Audited as chat:conversation_delete with the conversation id and its message count only (plan 9.6)."""
    async with chat_session_context(user.tenant_id, user.id) as db:
        message_count = await chat_conversations.delete_own(db, user, conversation_id)
        if message_count is None:
            raise document_scope.not_found()
        await record_audit_event(
            tenant_id=user.tenant_id,
            action="chat:conversation_delete",
            target_type="conversation",
            target_id=str(conversation_id),
            details={"conversation_id": str(conversation_id), "message_count": message_count},
            actor_id=user.id,
            request=request,
            db=db,
        )
    logger.info("chat_conversation_deleted", tenant_id=str(user.tenant_id), conversation_id=str(conversation_id),
                message_count=message_count)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
