"""
tests/test_chat_feedback.py
───────────────────────────
PUT and DELETE /api/v1/chat/messages/{message_id}/feedback (plan 8.1, 8.2, 9.1; step 6a-12a).

Rules checked here (owner, 6a-12a brief):
  - only an ASSISTANT message of the caller can be rated; anything else (a question, another user's answer, another
    tenant's, an id that does not exist) is the generic 404, the same body for every case;
  - the class scope is NOT re-checked: a student who left the class can still rate and un-rate their own answers;
  - PUT is an upsert (one row per user and answer), DELETE removes it and is idempotent (204 when there is none);
  - the comment is cleaned like a question (0-500 characters after cleaning; empty becomes null);
  - feedback shows up in the history, is written as a real row (plan exit criterion), is not audited (9.6) and never
    reaches the logs.
"""

import uuid
from typing import Any

import pytest
import structlog
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import GENERIC_NOT_FOUND
from app.db.models.academic import Enrollment
from app.db.models.audit import AuditLog
from app.db.models.chat import MessageFeedback
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.chat_world import conversation
from tests.fixtures.documents import auth
from tests.fixtures.history_world import (
    BASE_TIME,
    FEEDBACK_KEYS,
    Exchange,
    exchange,
    feedback_path,
    history_path,
)

NOT_FOUND = {"detail": GENERIC_NOT_FOUND}
TOO_LONG = {"detail": {"code": "comment_too_long", "message": "Your comment is too long. Please make it shorter."}}


async def _answer(raw_db: AsyncSession, w: WorldTenant, who: str = "s1", **kw: Any) -> Exchange:
    user = getattr(w, who)
    conv = await conversation(raw_db, w, user, **kw)
    return await exchange(raw_db, w, user, conv.id, "what is speed", "Speed is distance over time.", at=BASE_TIME)


async def _rows(raw_db: AsyncSession, message_id: uuid.UUID) -> list[MessageFeedback]:
    await raw_db.commit()
    res = await raw_db.execute(
        select(MessageFeedback).where(MessageFeedback.message_id == message_id).execution_options(populate_existing=True)
    )
    return list(res.scalars().all())


async def test_put_creates_then_updates_one_row(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    headers = auth(w, w.s1)
    first = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "up"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == FEEDBACK_KEYS and (body["rating"], body["comment"]) == ("up", None)
    rows = await _rows(raw_db, ex.answer.id)
    assert [(r.rating, r.comment, r.user_id) for r in rows] == [(1, None, w.s1.id)]
    row_id, first_updated = rows[0].id, rows[0].updated_at

    second = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "down", "comment": "wrong"})
    assert second.status_code == 200, second.text
    assert (second.json()["rating"], second.json()["comment"]) == ("down", "wrong")
    rows = await _rows(raw_db, ex.answer.id)
    assert [(r.id, r.rating, r.comment) for r in rows] == [(row_id, -1, "wrong")]
    assert rows[0].updated_at > first_updated


async def test_the_comment_is_cleaned_and_an_empty_one_is_null(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    headers = auth(w, w.s1)
    resp = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "up", "comment": "  good\u200b  answer\x00 "})
    assert resp.status_code == 200, resp.text
    stored = (await _rows(raw_db, ex.answer.id))[0].comment
    assert stored == resp.json()["comment"]
    assert stored is not None and "\x00" not in stored and "\u200b" not in stored and stored == stored.strip()
    for empty in ("", "   ", "\u200b\u200b"):
        resp = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "up", "comment": empty})
        assert resp.status_code == 200 and resp.json()["comment"] is None, empty
        assert (await _rows(raw_db, ex.answer.id))[0].comment is None


async def test_comment_length_is_500_after_cleaning(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    headers = auth(w, w.s1)
    ok = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "up", "comment": "x" * 500})
    assert ok.status_code == 200, ok.text
    over = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "down", "comment": "x" * 501})
    assert (over.status_code, over.json()) == (422, TOO_LONG)
    # 300 characters that clean_text (NFC) expands past 500 are refused, not cut and not a database error
    expands = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "down", "comment": "\u0958" * 300})
    assert (expands.status_code, expands.json()) == (422, TOO_LONG)
    assert [(r.rating, r.comment) for r in await _rows(raw_db, ex.answer.id)] == [(1, "x" * 500)]


@pytest.mark.parametrize("body", [
    {}, {"rating": "UP"}, {"rating": 1}, {"rating": "meh"}, {"rating": None}, {"rating": "up", "extra": 1},
    {"rating": "up", "comment": 5}, {"rating": "up", "comment": "x" * 4001}, {"rating": "up", "user_id": str(uuid.uuid4())},
])
async def test_a_bad_body_is_422_and_writes_nothing(
    body: dict[str, Any], client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    resp = await client.put(feedback_path(ex.answer.id), headers=auth(w, w.s1), json=body)
    assert resp.status_code == 422, resp.text
    assert await _rows(raw_db, ex.answer.id) == []


async def test_only_an_assistant_message_of_the_caller_can_be_rated(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    own = await _answer(raw_db, a)
    other_user = await _answer(raw_db, a, "s2", class_name="10-B", subject_name="Math")
    other_tenant = await _answer(raw_db, b)
    headers = auth(a, a.s1)
    for message_id in (own.question.id, other_user.answer.id, other_tenant.answer.id, uuid.uuid4()):
        for method, kwargs in (("PUT", {"json": {"rating": "up"}}), ("DELETE", {})):
            resp = await client.request(method, feedback_path(message_id), headers=headers, **kwargs)
            assert (resp.status_code, resp.json()) == (404, NOT_FOUND), (method, message_id)
    for w, user in ((a, a.admin), (a, a.t1), (a, a.s2), (b, b.admin), (b, b.s1)):
        resp = await client.put(feedback_path(own.answer.id), headers=auth(w, user), json={"rating": "down"})
        assert (resp.status_code, resp.json()) == (404, NOT_FOUND), user.email
        resp = await client.delete(feedback_path(own.answer.id), headers=auth(w, user))
        assert (resp.status_code, resp.json()) == (404, NOT_FOUND), user.email
    for message_id in (own.question.id, own.answer.id, other_user.answer.id, other_tenant.answer.id):
        assert await _rows(raw_db, message_id) == []


async def test_a_malformed_message_id_is_422(client: AsyncClient, academic_world: AcademicWorld) -> None:
    w = academic_world.a
    resp = await client.put(feedback_path("not-an-id"), headers=auth(w, w.s1), json={"rating": "up"})
    assert resp.status_code == 422


async def test_delete_removes_the_row_and_is_idempotent(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    headers = auth(w, w.s1)
    assert (await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "up"})).status_code == 200
    first = await client.delete(feedback_path(ex.answer.id), headers=headers)
    assert (first.status_code, first.content) == (204, b"")
    assert await _rows(raw_db, ex.answer.id) == []
    again = await client.delete(feedback_path(ex.answer.id), headers=headers)
    assert again.status_code == 204


async def test_a_student_who_left_the_class_can_still_rate_and_unrate(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    await raw_db.execute(delete(Enrollment).where(Enrollment.tenant_id == w.id, Enrollment.user_id == w.s1.id,
                                                  Enrollment.class_id == w.classes["10-A"].id))
    await raw_db.commit()
    headers = auth(w, w.s1)
    assert (await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "down"})).status_code == 200
    assert (await client.delete(feedback_path(ex.answer.id), headers=headers)).status_code == 204


async def test_feedback_is_shown_in_the_history(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    headers = auth(w, w.s1)
    put = await client.put(feedback_path(ex.answer.id), headers=headers, json={"rating": "down", "comment": "unclear"})
    items = (await client.get(history_path(ex.answer.conversation_id), headers=headers)).json()["items"]
    assert items[1]["feedback"] == put.json()
    await client.delete(feedback_path(ex.answer.id), headers=headers)
    items = (await client.get(history_path(ex.answer.conversation_id), headers=headers)).json()["items"]
    assert items[1]["feedback"] is None


async def test_feedback_is_not_audited_and_its_comment_never_reaches_the_logs(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    w = academic_world.a
    ex = await _answer(raw_db, w)
    tag = f"COMMENT-{uuid.uuid4().hex}"
    caplog.set_level("DEBUG")
    with structlog.testing.capture_logs() as captured:
        resp = await client.put(feedback_path(ex.answer.id), headers=auth(w, w.s1), json={"rating": "up", "comment": tag})
        await client.put(feedback_path(ex.answer.id), headers=auth(w, w.s1), json={"rating": "up", "comment": tag + "x" * 500})
        await client.delete(feedback_path(ex.answer.id), headers=auth(w, w.s1))
    assert resp.status_code == 200
    assert tag not in str(captured) and tag not in caplog.text
    await raw_db.commit()
    audits = (await raw_db.execute(select(AuditLog.action).where(AuditLog.tenant_id == w.id,
                                                                 AuditLog.action.like("chat:%")))).scalars().all()
    assert audits == []
