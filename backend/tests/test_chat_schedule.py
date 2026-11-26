"""
tests/test_chat_schedule.py
───────────────────────────
The schedule path of the message route end to end (step 6a-9b, plan 3.2, 3.5, 11.1): router -> schedule service ->
fixed template -> T3 update of the pending row. The plan's exit criteria: a student, a teacher and an admin (one class,
all classes) get their timetable answers with ZERO embedding, retrieval and LLM calls (the `no_model_calls` spy fails
the test on any). Answers are built only from the structured fields of the schedule service (class, subject, teacher,
room, start, end, weekday, date) and never show a class outside the caller's role scope or another tenant's data.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import chat_messages, chat_schedule, schedule
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.chat_world import (
    A_LECTURES,
    B_LECTURES,
    B_ROOM_CANARY,
    ROOM_CANARY,
    NoModelSpy,
    conversation,
    post_message,
    publish_timetables,
    rows,
)
from tests.fixtures.sse_client import parse_sse

pytestmark = pytest.mark.usefixtures("fixed_clock")


@pytest.fixture
async def timetables(academic_world: AcademicWorld, raw_db: AsyncSession) -> AcademicWorld:
    await publish_timetables(raw_db, academic_world.a, A_LECTURES)
    await publish_timetables(raw_db, academic_world.b, B_LECTURES)
    return academic_world


async def _ask(client: AsyncClient, raw_db: AsyncSession, w: WorldTenant, who: str, question: str,
               class_name: str = "10-A", subject: str | None = "Math") -> tuple[list[Any], Any]:
    user = getattr(w, who)
    conv = await conversation(raw_db, w, user, class_name, subject)
    resp = await post_message(client, w, user, conv.id, question)
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.content)
    stored = await rows(raw_db, conv.id)
    return events, stored


def _text(events: list[Any]) -> str:
    return "".join(e.data["text"] for e in events if e.name == "token")


def _names(events: list[Any]) -> list[str]:
    return [e.name for e in events if e.name != "ping"]


S1_NEXT = "Next lecture: Monday 2026-11-23\n- 10:00-11:00 Math (10-A), room R-101, World t1"
S1_TODAY = (
    "Monday 2026-11-23:\n"
    "- 10:00-11:00 Math (10-A), room R-101, World t1\n"
    f"- 11:00-12:00 Physics (10-A), room {ROOM_CANARY}, World t1\n"
    "- 12:00-13:00 Chemistry (11-A), room R-301, World t2"
)


@pytest.mark.parametrize(("question", "intent", "expected"), [
    ("what is my next lecture", "next_lecture", S1_NEXT),
    ("today's timetable", "today", S1_TODAY),
    ("tomorrow's schedule", "tomorrow", "Tuesday 2026-11-24:\n- 09:00-10:00 English (10-A)"),
    ("wednesday timetable", "weekday", "Wednesday 2026-11-25:\n- 09:00-10:00 English (11-A), room R-302"),
    ("what's on now", "now", "No lecture is on right now."),
    ("when is physics", "subject_when", f"Physics:\n- Monday: 11:00-12:00 Physics (10-A), room {ROOM_CANARY}, World t1"),
    ("which room is chemistry", "room", "Chemistry:\n- Monday: 12:00-13:00 Chemistry (11-A), room R-301, World t2"),
    ("when is english", "subject_when",
     "English:\n- Tuesday: 09:00-10:00 English (10-A)\n- Wednesday: 09:00-10:00 English (11-A), room R-302"),
])
async def test_student_schedule_answers_with_zero_model_calls(
    question: str, intent: str, expected: str, client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession,
    no_model_calls: NoModelSpy,
) -> None:
    w = timetables.a
    events, stored = await _ask(client, raw_db, w, "s1", question)
    assert _names(events) == ["meta", "token", "done"]
    assert events[0].data["route"] == "schedule" and events[0].data["language"] == "en"
    assert _text(events) == expected
    done = events[-1].data
    assert done["outcome"] == "schedule_answered" and done["partial"] is False
    assert done["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False}
    question_row, answer = stored
    assert answer.content == expected and answer.outcome == "schedule_answered" and answer.intent == intent
    assert answer.route == "schedule" and answer.router_confidence == "high" and answer.language == "en"
    assert answer.rule_ids and answer.rule_ids[-1] == f"intent.{intent}"
    assert answer.provider is None and answer.model_key is None and answer.prompt_tokens is None
    # 10-B is not S1's class: its Physics lecture and room never appear
    assert "R-201" not in expected and B_ROOM_CANARY not in answer.content


async def test_now_during_a_lecture(client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession,
                                    monkeypatch: pytest.MonkeyPatch, no_model_calls: NoModelSpy) -> None:
    monkeypatch.setattr(chat_messages, "clock", lambda: datetime(2026, 11, 23, 5, 0, tzinfo=UTC))  # 10:30 IST
    events, _ = await _ask(client, raw_db, timetables.a, "s1", "what's on now")
    assert _text(events) == "On now:\n- 10:00-11:00 Math (10-A), room R-101, World t1"


async def test_marathi_question_gets_the_marathi_template(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    events, stored = await _ask(client, raw_db, timetables.a, "s1", "पुढचा तास कधी आहे")
    assert events[0].data["language"] == "mr"
    assert _text(events) == "पुढचा तास: सोमवार 2026-11-23\n- 10:00-11:00 Math (10-A), खोली R-101, World t1"
    assert stored[1].language == "mr"


async def test_romanised_marathi_is_answered_with_the_english_template(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    events, _ = await _ask(client, raw_db, timetables.a, "s1", "maza next lecture kadhi aahe")
    assert events[0].data["language"] == "en" and _text(events) == S1_NEXT


async def test_marathi_falls_back_to_english_templates_when_marathi_is_not_enabled(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "CHAT_LANGUAGES", "en")
    events, stored = await _ask(client, raw_db, timetables.a, "s1", "पुढचा तास कधी आहे")
    assert events[0].data["language"] == "mr" and stored[1].language == "mr"  # detected and stored as it is
    assert _text(events) == S1_NEXT  # but the template is English


async def test_teacher_sees_own_lectures_and_assigned_classes(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    events, _ = await _ask(client, raw_db, timetables.a, "t1", "today's timetable")
    assert _text(events) == (
        "Monday 2026-11-23:\n"
        "- 10:00-11:00 Math (10-A), room R-101, World t1\n"
        "- 10:00-11:00 Physics (10-B), room R-201, World t2\n"
        f"- 11:00-12:00 Physics (10-A), room {ROOM_CANARY}, World t1"
    )


async def test_admin_one_class(client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession,
                               no_model_calls: NoModelSpy) -> None:
    events, stored = await _ask(client, raw_db, timetables.a, "admin", "next lecture for 10-B")
    assert _text(events) == "Next lecture: Monday 2026-11-23\n- 10:00-11:00 Physics (10-B), room R-201, World t2"
    assert stored[1].intent == "next_lecture"


async def test_admin_all_classes(client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession,
                                 no_model_calls: NoModelSpy) -> None:
    events, stored = await _ask(client, raw_db, timetables.a, "admin", "timetable for all classes")
    assert _text(events) == (
        "10-A:\n"
        "- Monday: 10:00-11:00 Math (10-A), room R-101, World t1\n"
        f"- Monday: 11:00-12:00 Physics (10-A), room {ROOM_CANARY}, World t1\n"
        "- Tuesday: 09:00-10:00 English (10-A)\n"
        "10-B:\n"
        "- Monday: 10:00-11:00 Physics (10-B), room R-201, World t2\n"
        "11-A:\n"
        "- Monday: 12:00-13:00 Chemistry (11-A), room R-301, World t2\n"
        "- Wednesday: 09:00-10:00 English (11-A), room R-302"
    )
    assert stored[1].intent == "class_named" and B_ROOM_CANARY not in stored[1].content


async def test_two_classes_named_gives_clarify_with_authorised_names_only(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    events, stored = await _ask(client, raw_db, timetables.a, "admin", "next lecture for 10-A and 10-B")
    assert events[0].data["route"] == "clarify"
    assert _text(events) == "Which one do you mean: 10-A, 10-B?"
    assert events[-1].data["outcome"] == "clarify"
    assert (stored[1].route, stored[1].outcome) == ("clarify", "clarify")


async def test_no_published_timetable_is_its_own_template(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    w = academic_world.a
    for question in ("what is my next lecture", "today's timetable", "when is physics", "what's on now"):
        events, stored = await _ask(client, raw_db, w, "s1", question)
        assert _text(events) == "No timetable has been published for your class yet.", question
        assert events[-1].data["outcome"] == "schedule_no_timetable"
        assert stored[1].outcome == "schedule_no_timetable"


async def test_marathi_no_timetable_text(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession,
                                         no_model_calls: NoModelSpy) -> None:
    events, _ = await _ask(client, raw_db, academic_world.a, "s1", "आजचे वेळापत्रक काय आहे")
    assert events[0].data["language"] == "mr"
    assert _text(events) == "तुमच्या वर्गासाठी अजून वेळापत्रक प्रकाशित झालेले नाही."


async def test_devanagari_without_a_function_word_ties_and_gets_the_default_language(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    """6a-7 rule (plan 5.7): when the word lists tie, the default language wins, even when it is English."""
    events, _ = await _ask(client, raw_db, academic_world.a, "s1", "आजचे वेळापत्रक")
    assert events[0].data["language"] == "en"
    assert _text(events) == "No timetable has been published for your class yet."


async def test_a_schedule_service_failure_is_schedule_unavailable_and_never_reaches_a_model(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("database went away")

    monkeypatch.setattr(schedule, "next_lecture", broken)
    events, stored = await _ask(client, raw_db, timetables.a, "s1", "what is my next lecture")
    assert _names(events) == ["meta", "error"]
    assert events[-1].data == {"code": "schedule_unavailable",
                               "message": chat_messages.STREAM_ERROR_MESSAGES["en"]["schedule_unavailable"],
                               "partial": False}
    assert stored[1].outcome == "error" and stored[1].content == "" and stored[1].completed_at is not None


async def test_other_tenant_timetables_never_appear(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    b = timetables.b
    events, _ = await _ask(client, raw_db, b, "admin", "timetable for all classes")
    text = _text(events)
    assert B_ROOM_CANARY in text and ROOM_CANARY not in text and "R-101" not in text


async def test_a_student_naming_a_subject_outside_their_classes_is_not_matched(
    client: AsyncClient, timetables: AcademicWorld, raw_db: AsyncSession, no_model_calls: NoModelSpy
) -> None:
    """S2's class 10-B offers no Chemistry: the name is not in S2's router context, so it is an unexplained word and
    the question is not a timetable question at all (it cannot be used to probe which subjects exist)."""
    events, stored = await _ask(client, raw_db, timetables.a, "s2", "when is chemistry", "10-B", None)
    assert events[0].data["route"] == "rag"
    assert stored[1].route == "rag" and "name.subject" not in (stored[1].rule_ids or [])


def test_templates_exist_for_every_enabled_language_and_hold_the_same_keys() -> None:
    assert set(chat_schedule.TEMPLATES) == {"en", "mr"}
    assert set(chat_schedule.TEMPLATES["en"]) == set(chat_schedule.TEMPLATES["mr"])
    assert len(chat_schedule.DAY_NAMES["mr"]) == len(chat_schedule.DAY_NAMES["en"]) == 7
