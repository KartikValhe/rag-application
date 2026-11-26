"""
tests/test_chat_history.py
──────────────────────────
GET /api/v1/chat/conversations/{conversation_id}/messages: the message history (plan 8.1, 8.2; step 6a-12a).

Owner decisions checked here (6a-12 brief, decision 1; 6a-12a brief):
  - only the caller's own conversation; every other id is the generic 404 (no admin or teacher reading, Q4);
  - the caller's CURRENT read scope for the conversation's class and subject is re-checked on every call (generic 404
    when out of scope), unlike list, get and delete (6a-9b decision, plan 4.2);
  - every role gets the student citation shape: never a document, version or chunk id, a file kind, a score or a
    flag mark (staff shape deferred);
  - a pending answer older than CHAT_PENDING_STALE_SECONDS reads as interrupted (C28; nothing is written);
  - a blanked snippet (C21: '') reads as empty text, never as an error;
  - paging with before=<message id>, oldest first in a page; a malformed id or a message that is not in this
    conversation is the same 422 invalid_cursor.
"""

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import structlog
from httpx import AsyncClient
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import GENERIC_NOT_FOUND
from app.core.config import settings
from app.db.engine import engine as app_engine
from app.db.models.academic import ClassSubject, Enrollment
from app.db.models.chat import Message
from app.db.models.tenant_settings import TenantSettings
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.chat_world import conversation
from tests.fixtures.documents import auth
from tests.fixtures.history_world import (
    BASE_TIME,
    CITATION_KEYS,
    FEEDBACK_KEYS,
    MESSAGE_KEYS,
    citation,
    exchange,
    feedback,
    history_path,
    ids,
    set_age,
)

NOT_FOUND = {"detail": GENERIC_NOT_FOUND}
INVALID_CURSOR = {"detail": {"code": "invalid_cursor", "message": "This list link is not valid any more. Reload the list."}}


# ── Shape ─────────────────────────────────────────────────────────────────────


async def test_history_returns_the_callers_messages_oldest_first_with_citations_and_own_feedback(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    one = await exchange(raw_db, w, w.s1, conv.id, "what is speed", "Speed is distance over time [S1].", at=BASE_TIME)
    await citation(raw_db, w, w.s1, one.answer, label="S1", ordinal=1, flagged=True)
    await citation(raw_db, w, w.s1, one.answer, label="S2", ordinal=2, title="Slides", file_kind="pptx",
                   location_kind="slide", page_start=5, page_end=5, heading_path=None, snippet="Slide text")
    await feedback(raw_db, w, w.s1, one.answer, rating=-1, comment="too short")
    two = await exchange(raw_db, w, w.s1, conv.id, "next lecture", "Math at 10:00.", at=BASE_TIME + timedelta(minutes=1),
                         outcome="schedule_answered", route="schedule", language="mr")

    resp = await client.get(history_path(conv.id), headers=auth(w, w.s1))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "has_more"}
    assert body["has_more"] is False
    items = body["items"]
    assert ids(items) == [str(one.question.id), str(one.answer.id), str(two.question.id), str(two.answer.id)]
    assert all(set(item) == MESSAGE_KEYS for item in items)

    q, a = items[0], items[1]
    assert q == {
        "id": str(one.question.id), "role": "user", "content": "what is speed", "created_at": q["created_at"],
        "route": None, "outcome": None, "language": None, "citations": [], "feedback": None, "partial": False,
    }
    assert a["role"] == "assistant" and a["content"] == "Speed is distance over time [S1]."
    assert (a["route"], a["outcome"], a["language"], a["partial"]) == ("rag", "answered", "en", False)
    assert [c["label"] for c in a["citations"]] == ["S1", "S2"]
    assert all(set(c) == CITATION_KEYS for c in a["citations"])
    assert a["citations"][0] == {
        "label": "S1", "document_title": "Intro to Physics", "location_kind": "page", "page_start": 3, "page_end": 3,
        "heading_path": "Motion > Speed", "snippet": "Speed is distance over time.",
    }
    assert a["citations"][1]["location_kind"] == "slide" and a["citations"][1]["heading_path"] is None
    assert set(a["feedback"]) == FEEDBACK_KEYS
    assert (a["feedback"]["rating"], a["feedback"]["comment"]) == ("down", "too short")
    assert items[3]["feedback"] is None and items[3]["citations"] == []
    assert (items[3]["route"], items[3]["outcome"], items[3]["language"]) == ("schedule", "schedule_answered", "mr")


@pytest.mark.parametrize("who", ["s1", "t1", "admin"])
async def test_every_role_gets_the_student_citation_shape_and_no_ids_scores_or_flags(
    who: str, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    user = getattr(w, who)
    conv = await conversation(raw_db, w, user)
    ex = await exchange(raw_db, w, user, conv.id, "q", "a [S1]", at=BASE_TIME)
    cit = await citation(raw_db, w, user, ex.answer, flagged=True)
    resp = await client.get(history_path(conv.id), headers=auth(w, user))
    assert resp.status_code == 200, resp.text
    text = resp.text
    for secret in (str(cit.document_id), str(cit.version_id), str(cit.chunk_id), "0.91", "flagged", "pdf", "similarity"):
        assert secret not in text, secret
    assert set(resp.json()["items"][1]["citations"][0]) == CITATION_KEYS


async def test_a_blanked_snippet_reads_as_empty_text(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    ex = await exchange(raw_db, w, w.s1, conv.id, "q", "a [S1]", at=BASE_TIME)
    await citation(raw_db, w, w.s1, ex.answer, snippet="", heading_path="")
    resp = await client.get(history_path(conv.id), headers=auth(w, w.s1))
    assert resp.status_code == 200, resp.text
    cit = resp.json()["items"][1]["citations"][0]
    assert (cit["snippet"], cit["heading_path"]) == ("", "")


# ── Pending rows (C28) ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("age", "outcome", "partial"), [(119, "pending", False), (121, "interrupted", True)])
async def test_a_pending_answer_reads_as_interrupted_only_after_the_stale_age(
    age: int, outcome: str, partial: bool, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    assert settings.CHAT_PENDING_STALE_SECONDS == 120
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    ex = await exchange(raw_db, w, w.s1, conv.id, "q", "", at=BASE_TIME, outcome="pending")
    await set_age(raw_db, ex.answer.id, age)
    resp = await client.get(history_path(conv.id), headers=auth(w, w.s1))
    assert resp.status_code == 200, resp.text
    answer = resp.json()["items"][-1]
    assert (answer["outcome"], answer["partial"], answer["content"]) == (outcome, partial, "")
    # Read time only: the row itself is not touched
    await raw_db.commit()
    stored = (await raw_db.execute(select(Message.outcome, Message.partial).where(Message.id == ex.answer.id))).one()
    assert tuple(stored) == ("pending", False)


async def test_a_finished_answer_keeps_its_outcome_whatever_its_age(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    ex = await exchange(raw_db, w, w.s1, conv.id, "q", "a", at=BASE_TIME, outcome="cancelled", partial=True)
    await set_age(raw_db, ex.answer.id, 10_000)
    answer = (await client.get(history_path(conv.id), headers=auth(w, w.s1))).json()["items"][-1]
    assert (answer["outcome"], answer["partial"]) == ("cancelled", True)


# ── Paging ────────────────────────────────────────────────────────────────────


async def _many(raw_db: AsyncSession, academic_world: AcademicWorld, count: int) -> tuple[uuid.UUID, list[str]]:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    order: list[str] = []
    for i in range(count):
        ex = await exchange(raw_db, w, w.s1, conv.id, f"q{i}", f"a{i}", at=BASE_TIME + timedelta(minutes=i))
        order += [str(ex.question.id), str(ex.answer.id)]
    return conv.id, order


async def test_pages_walk_back_with_before_and_never_overlap(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv_id, order = await _many(raw_db, academic_world, 4)  # 8 rows
    headers = auth(w, w.s1)
    first = (await client.get(history_path(conv_id), params={"limit": 3}, headers=headers)).json()
    assert ids(first["items"]) == order[5:] and first["has_more"] is True
    second = (await client.get(history_path(conv_id), params={"limit": 3, "before": order[5]}, headers=headers)).json()
    assert ids(second["items"]) == order[2:5] and second["has_more"] is True
    third = (await client.get(history_path(conv_id), params={"limit": 3, "before": order[2]}, headers=headers)).json()
    assert ids(third["items"]) == order[:2] and third["has_more"] is False
    last = (await client.get(history_path(conv_id), params={"limit": 3, "before": order[0]}, headers=headers)).json()
    assert last == {"items": [], "has_more": False}


async def test_has_more_is_false_when_the_oldest_page_is_exactly_full(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv_id, order = await _many(raw_db, academic_world, 3)  # 6 rows: two full pages of 3
    headers = auth(w, w.s1)
    first = (await client.get(history_path(conv_id), params={"limit": 3}, headers=headers)).json()
    assert ids(first["items"]) == order[3:] and first["has_more"] is True
    second = (await client.get(history_path(conv_id), params={"limit": 3, "before": order[3]}, headers=headers)).json()
    assert ids(second["items"]) == order[:3] and second["has_more"] is False
    whole = (await client.get(history_path(conv_id), params={"limit": 6}, headers=headers)).json()
    assert ids(whole["items"]) == order and whole["has_more"] is False


async def test_rows_with_the_same_time_are_paged_by_id_without_loss(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv_id, _ = await _many(raw_db, academic_world, 3)
    await raw_db.execute(update(Message).where(Message.conversation_id == conv_id).values(created_at=BASE_TIME))
    await raw_db.commit()
    expected = sorted(
        [str(i) for i in (await raw_db.execute(select(Message.id).where(Message.conversation_id == conv_id))).scalars()]
    )
    headers = auth(w, w.s1)
    seen: list[str] = []
    before: str | None = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 2} | ({"before": before} if before else {})
        page = (await client.get(history_path(conv_id), params=params, headers=headers)).json()
        seen = ids(page["items"]) + seen
        if not page["has_more"]:
            break
        before = page["items"][0]["id"]
    assert seen == expected  # one time for all: oldest first means id order, nothing lost or repeated


async def test_the_default_page_is_30_and_the_limit_is_1_to_50(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv_id, order = await _many(raw_db, academic_world, 16)  # 32 rows
    headers = auth(w, w.s1)
    page = (await client.get(history_path(conv_id), headers=headers)).json()
    assert ids(page["items"]) == order[2:] and page["has_more"] is True
    assert len((await client.get(history_path(conv_id), params={"limit": 50}, headers=headers)).json()["items"]) == 32
    for bad in ("0", "51", "-1", "x"):
        resp = await client.get(history_path(conv_id), params={"limit": bad}, headers=headers)
        assert resp.status_code == 422, (bad, resp.text)


@pytest.mark.parametrize("bad", [
    "abc", "", " ", "0" * 37, "not-a-uuid-at-all-not-a-uuid-at-all!",
])
async def test_a_malformed_before_is_invalid_cursor(
    bad: str, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv_id, _ = await _many(raw_db, academic_world, 1)
    resp = await client.get(history_path(conv_id), params={"before": bad}, headers=auth(w, w.s1))
    assert (resp.status_code, resp.json()) == (422, INVALID_CURSOR), bad


async def test_a_non_canonical_spelling_of_a_real_id_is_invalid_cursor(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv_id, order = await _many(raw_db, academic_world, 2)
    real = uuid.UUID(order[-1])
    for spelling in (str(real).upper(), real.hex, "{" + str(real) + "}", f"urn:uuid:{real}", f" {real}"):
        resp = await client.get(history_path(conv_id), params={"before": spelling}, headers=auth(w, w.s1))
        assert (resp.status_code, resp.json()) == (422, INVALID_CURSOR), spelling
    ok = await client.get(history_path(conv_id), params={"before": str(real)}, headers=auth(w, w.s1))
    assert ok.status_code == 200, ok.text


async def test_a_message_that_is_not_in_this_conversation_is_invalid_cursor(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    conv = await conversation(raw_db, a, a.s1)
    await exchange(raw_db, a, a.s1, conv.id, "q", "a", at=BASE_TIME)
    own_other = await conversation(raw_db, a, a.s1, "11-A", "Chemistry")
    in_other_conv = await exchange(raw_db, a, a.s1, own_other.id, "q", "a", at=BASE_TIME)
    other_user_conv = await conversation(raw_db, a, a.s2, "10-B", "Math")
    other_user = await exchange(raw_db, a, a.s2, other_user_conv.id, "q", "a", at=BASE_TIME)
    b_conv = await conversation(raw_db, b, b.s1)
    other_tenant = await exchange(raw_db, b, b.s1, b_conv.id, "q", "a", at=BASE_TIME)
    for before in (in_other_conv.answer.id, other_user.answer.id, other_tenant.answer.id, uuid.uuid4()):
        resp = await client.get(history_path(conv.id), params={"before": str(before)}, headers=auth(a, a.s1))
        assert (resp.status_code, resp.json()) == (422, INVALID_CURSOR), before


# ── Who may read ──────────────────────────────────────────────────────────────


async def test_another_users_conversation_is_the_generic_404_for_everybody(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    conv = await conversation(raw_db, a, a.s1)
    await exchange(raw_db, a, a.s1, conv.id, "q", "a", at=BASE_TIME)
    for w, user in ((a, a.admin), (a, a.t1), (a, a.t2), (a, a.s2), (b, b.s1), (b, b.admin)):
        resp = await client.get(history_path(conv.id), headers=auth(w, user))
        assert (resp.status_code, resp.json()) == (404, NOT_FOUND), user.email
    resp = await client.get(history_path(uuid.uuid4()), headers=auth(a, a.s1))
    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)


async def test_the_404_comes_before_the_cursor_check(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a = academic_world.a
    conv = await conversation(raw_db, a, a.s1)
    resp = await client.get(history_path(conv.id), params={"before": "junk"}, headers=auth(a, a.s2))
    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)


async def test_a_student_who_left_the_class_gets_404_on_history_but_keeps_metadata_and_delete(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    await exchange(raw_db, w, w.s1, conv.id, "q", "a", at=BASE_TIME)
    headers = auth(w, w.s1)
    assert (await client.get(history_path(conv.id), headers=headers)).status_code == 200
    await raw_db.execute(delete(Enrollment).where(Enrollment.tenant_id == w.id, Enrollment.user_id == w.s1.id,
                                                  Enrollment.class_id == w.classes["10-A"].id))
    await raw_db.commit()
    resp = await client.get(history_path(conv.id), headers=headers)
    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)
    assert (await client.get(f"/api/v1/chat/conversations/{conv.id}", headers=headers)).status_code == 200
    assert (await client.delete(f"/api/v1/chat/conversations/{conv.id}", headers=headers)).status_code == 204


async def test_a_subject_no_longer_offered_in_the_class_gives_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1, "10-A", "English")
    await exchange(raw_db, w, w.s1, conv.id, "q", "a", at=BASE_TIME)
    assert (await client.get(history_path(conv.id), headers=auth(w, w.s1))).status_code == 200
    await raw_db.execute(delete(ClassSubject).where(ClassSubject.tenant_id == w.id,
                                                    ClassSubject.class_id == w.classes["10-A"].id,
                                                    ClassSubject.subject_id == w.subjects["English"].id))
    await raw_db.commit()
    resp = await client.get(history_path(conv.id), headers=auth(w, w.s1))
    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)


async def test_a_teacher_sees_their_own_preview_chat_only_while_they_teach_the_pair(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    own = await conversation(raw_db, w, w.t1, "10-A", "Math")
    outside = await conversation(raw_db, w, w.t1, "10-A", "English")  # T1 does not teach 10-A English
    headers = auth(w, w.t1)
    assert (await client.get(history_path(own.id), headers=headers)).status_code == 200
    resp = await client.get(history_path(outside.id), headers=headers)
    assert (resp.status_code, resp.json()) == (404, NOT_FOUND)


async def test_history_still_works_when_chat_is_switched_off(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    raw_db.add(TenantSettings(tenant_id=w.id, chat_enabled=False))
    await raw_db.commit()
    conv = await conversation(raw_db, w, w.s1)
    await exchange(raw_db, w, w.s1, conv.id, "q", "a", at=BASE_TIME)
    resp = await client.get(history_path(conv.id), headers=auth(w, w.s1))
    assert resp.status_code == 200 and len(resp.json()["items"]) == 2


# ── Connections and logs ──────────────────────────────────────────────────────


class _PoolWatch:
    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    def checkout(self, *_a: Any) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)

    def checkin(self, *_a: Any) -> None:
        self.current -= 1


@pytest.fixture
def pool_watch() -> Iterator[_PoolWatch]:
    watch = _PoolWatch()
    event.listen(app_engine.sync_engine, "checkout", watch.checkout)
    event.listen(app_engine.sync_engine, "checkin", watch.checkin)
    try:
        yield watch
    finally:
        event.remove(app_engine.sync_engine, "checkout", watch.checkout)
        event.remove(app_engine.sync_engine, "checkin", watch.checkin)


async def test_history_never_holds_two_connections_and_returns_them_all(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, pool_watch: _PoolWatch
) -> None:
    w = academic_world.a
    conv = await conversation(raw_db, w, w.s1)
    await exchange(raw_db, w, w.s1, conv.id, "q", "a", at=BASE_TIME)
    assert (await client.get(history_path(conv.id), headers=auth(w, w.s1))).status_code == 200
    assert pool_watch.peak == 1, f"{pool_watch.peak} connections were checked out at the same time"
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]


async def test_no_question_answer_title_heading_or_snippet_reaches_the_logs(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    w = academic_world.a
    tag = uuid.uuid4().hex
    conv = await conversation(raw_db, w, w.s1)
    ex = await exchange(raw_db, w, w.s1, conv.id, f"Q-{tag}", f"A-{tag} [S1]", at=BASE_TIME)
    await citation(raw_db, w, w.s1, ex.answer, title=f"T-{tag}", heading_path=f"H-{tag}", snippet=f"N-{tag}")
    await feedback(raw_db, w, w.s1, ex.answer, comment=f"C-{tag}")
    caplog.set_level("DEBUG")
    with structlog.testing.capture_logs() as captured:
        resp = await client.get(history_path(conv.id), headers=auth(w, w.s1))
        await client.get(history_path(conv.id), params={"before": "junk"}, headers=auth(w, w.s1))
    assert resp.status_code == 200 and tag in resp.text
    assert tag not in str(captured) and tag not in caplog.text
