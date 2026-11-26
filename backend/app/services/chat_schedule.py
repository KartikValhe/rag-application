"""
app/services/chat_schedule.py
─────────────────────────────
Timetable answers of the chat (docs/phase6-plan.md 3.2, 3.5, step 6a-9b): a router decision of route "schedule" or
"clarify" becomes one fixed-template text. No embedding, no retrieval and no LLM: the text is built ONLY from the
structured fields that app/services/schedule.py returns (class, subject, teacher, room, start, end, weekday, date),
and who may see which class is decided by that service, never here (student: enrolled classes, all labelled;
teacher: own lectures and assigned classes; admin: one class or all).

  answer(db, user, decision, ctx, language, now) -> ScheduleAnswer     runs in the caller's short transaction (T2)
  clarify_answer(decision, ctx, language)        -> ScheduleAnswer     pure

Templates exist in English and Marathi (Q5: Hindi has none, it falls back to English). The Marathi texts are not
yet reviewed by a native speaker: that written sign-off is a pilot gate (plan 4.4, step 6a-13).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.user import User
from app.schemas.timetable import ScheduleItem
from app.services import schedule
from app.services.chat_router import RouteDecision, RouterContext

OUTCOME_ANSWERED: Final = "schedule_answered"
OUTCOME_NO_TIMETABLE: Final = "schedule_no_timetable"
OUTCOME_CLARIFY: Final = "clarify"

DAY_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "mr": ("सोमवार", "मंगळवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"),
}

TEMPLATES: Final[dict[str, dict[str, str]]] = {
    "en": {
        "item": "{start}-{end} {subject} ({klass})",
        "room": ", room {room}",
        "room_not_set": ", room not set",
        "teacher": ", {teacher}",
        "next_header": "Next lecture: {day} {date}",
        "now_header": "On now:",
        "nothing_now": "No lecture is on right now.",
        "no_upcoming": "No upcoming lectures are scheduled.",
        "day_header": "{day} {date}:",
        "day_empty": "No lectures on {day} {date}.",
        "subject_header": "{subject}:",
        "subject_day_header": "{subject}, {day} {date}:",
        "subject_empty": "No {subject} lectures were found in your timetable.",
        "class_header": "{klass}:",
        "class_empty": "{klass}: no lectures.",
        "no_timetable": "No timetable has been published for your class yet.",
        "clarify": "Which one do you mean: {names}?",
    },
    "mr": {
        "item": "{start}-{end} {subject} ({klass})",
        "room": ", खोली {room}",
        "room_not_set": ", खोली ठरलेली नाही",
        "teacher": ", {teacher}",
        "next_header": "पुढचा तास: {day} {date}",
        "now_header": "आत्ता सुरू:",
        "nothing_now": "आत्ता कोणताही तास सुरू नाही.",
        "no_upcoming": "पुढे कोणतेही तास नियोजित नाहीत.",
        "day_header": "{day} {date}:",
        "day_empty": "{day} {date} रोजी कोणतेही तास नाहीत.",
        "subject_header": "{subject}:",
        "subject_day_header": "{subject}, {day} {date}:",
        "subject_empty": "तुमच्या वेळापत्रकात {subject} चे तास सापडले नाहीत.",
        "class_header": "{klass}:",
        "class_empty": "{klass}: कोणतेही तास नाहीत.",
        "no_timetable": "तुमच्या वर्गासाठी अजून वेळापत्रक प्रकाशित झालेले नाही.",
        "clarify": "तुम्हाला कोणते म्हणायचे आहे: {names}?",
    },
}


@dataclass(frozen=True)
class ScheduleAnswer:
    text: str
    outcome: str


def template_language(detected: str) -> str:
    """The template language for a detected language: itself when enabled (CHAT_LANGUAGES) and templated, else en."""
    return detected if detected in TEMPLATES and detected in settings.chat_languages else "en"


class _Writer:
    def __init__(self, language: str) -> None:
        self.t = TEMPLATES[language]
        self.days = DAY_NAMES[language]

    def day(self, weekday: int) -> str:
        return self.days[weekday]

    def item(self, i: ScheduleItem, *, room_always: bool = False) -> str:
        out = self.t["item"].format(start=i.start_time[:5], end=i.end_time[:5], subject=i.subject_name, klass=i.class_name)
        if i.room:
            out += self.t["room"].format(room=i.room)
        elif room_always:
            out += self.t["room_not_set"]
        if i.teacher_name:
            out += self.t["teacher"].format(teacher=i.teacher_name)
        return out

    def lines(self, header: str, items: Iterable[str]) -> str:
        return "\n".join([header, *(f"- {line}" for line in items)])


def _ordered(items: Iterable[ScheduleItem]) -> list[ScheduleItem]:
    """Stable order: day, start, class, subject (the service orders by day and start only)."""
    return sorted(items, key=lambda i: (i.day_of_week, i.start_time, i.class_name, i.subject_name, i.end_time))


def _class_targets(decision: RouteDecision, ctx: RouterContext) -> list[uuid.UUID | None]:
    """The class argument of the service calls: the one named class, or None for "all of the caller's classes"
    (the service then applies the role rules)."""
    return [decision.class_ids[0]] if decision.class_ids else [None]


async def _no_timetable(db: AsyncSession, user: User, class_id: uuid.UUID | None, now: datetime) -> bool:
    """True when no published timetable exists in the caller's scope (the service's own answer for that case)."""
    probe = await schedule.next_lecture(user, now=now, class_id=class_id, db=db)
    return probe.status == "no_published_timetable"


async def _weekly(db: AsyncSession, user: User, class_ids: Iterable[uuid.UUID], now: datetime) -> list[ScheduleItem]:
    items: list[ScheduleItem] = []
    for class_id in class_ids:
        grouped = await schedule.class_schedule(user, class_id, db=db, now=now)
        for day_items in grouped.values():
            items.extend(day_items)
    return items


def _weekly_classes(decision: RouteDecision, ctx: RouterContext) -> list[uuid.UUID]:
    if decision.class_ids:
        return list(decision.class_ids)
    return [c.id for c in sorted(ctx.classes, key=lambda c: c.name)]


def clarify_answer(decision: RouteDecision, ctx: RouterContext, language: str) -> ScheduleAnswer:
    """Two or more classes or subjects matched: list the matching AUTHORISED names only (they come from `ctx`)."""
    if decision.subject_ids:
        known = {s.id: s.name for s in ctx.subjects}
        names = [known[i] for i in decision.subject_ids if i in known]
    else:
        known = {c.id: c.name for c in ctx.classes}
        names = [known[i] for i in decision.class_ids or () if i in known]
    return ScheduleAnswer(TEMPLATES[language]["clarify"].format(names=", ".join(names)), OUTCOME_CLARIFY)


async def answer(
    db: AsyncSession, user: User, decision: RouteDecision, ctx: RouterContext, language: str, now: datetime
) -> ScheduleAnswer:
    """The fixed-template answer of one schedule decision. Raises whatever the schedule service raises; the caller
    turns it into schedule_unavailable (never a fallback to the LLM, plan 3.5)."""
    w = _Writer(language)
    intent = decision.intent
    target = _class_targets(decision, ctx)[0]
    subject_name = next((s.name for s in ctx.subjects if s.id == decision.subject_id), "")

    if intent == "next_lecture":
        nl = await schedule.next_lecture(user, now=now, class_id=target, db=db)
        if nl.status == "no_published_timetable":
            return ScheduleAnswer(w.t["no_timetable"], OUTCOME_NO_TIMETABLE)
        parts: list[str] = []
        if nl.current_lecture is not None:
            parts.append(w.lines(w.t["now_header"], [w.item(nl.current_lecture)]))
        if nl.next_lecture is not None:
            nxt = nl.next_lecture
            parts.append(w.lines(w.t["next_header"].format(day=w.day(nxt.day_of_week), date=nxt.date), [w.item(nxt)]))
        else:
            parts.append(w.t["no_upcoming"])
        return ScheduleAnswer("\n".join(parts), OUTCOME_ANSWERED)

    if intent == "now":
        items = _ordered(await schedule.whats_on_now(user, now=now, class_id=target, db=db))
        if items:
            return ScheduleAnswer(w.lines(w.t["now_header"], [w.item(i) for i in items]), OUTCOME_ANSWERED)
        if await _no_timetable(db, user, target, now):
            return ScheduleAnswer(w.t["no_timetable"], OUTCOME_NO_TIMETABLE)
        return ScheduleAnswer(w.t["nothing_now"], OUTCOME_ANSWERED)

    if intent in ("today", "tomorrow", "weekday"):
        day: date = decision.day or schedule.resolve_reference_time(now, ctx.timezone).date()  # type: ignore[arg-type]
        items = _ordered(await schedule.schedule_for_day(user, day, class_id=target, db=db, now=now))
        label = {"day": w.day(day.weekday()), "date": day.isoformat()}
        if items:
            return ScheduleAnswer(w.lines(w.t["day_header"].format(**label), [w.item(i) for i in items]), OUTCOME_ANSWERED)
        if await _no_timetable(db, user, target, now):
            return ScheduleAnswer(w.t["no_timetable"], OUTCOME_NO_TIMETABLE)
        return ScheduleAnswer(w.t["day_empty"].format(**label), OUTCOME_ANSWERED)

    if intent in ("subject_when", "room"):
        room_always = intent == "room"
        if decision.day is not None:
            day_items = await schedule.schedule_for_day(user, decision.day, class_id=target, db=db, now=now)
            items = _ordered(i for i in day_items if i.subject_id == decision.subject_id)
            header = w.t["subject_day_header"].format(
                subject=subject_name, day=w.day(decision.day.weekday()), date=decision.day.isoformat())
            lines = [w.item(i, room_always=room_always) for i in items]
        else:
            weekly = await _weekly(db, user, _weekly_classes(decision, ctx), now)
            items = _ordered(i for i in weekly if i.subject_id == decision.subject_id)
            header = w.t["subject_header"].format(subject=subject_name)
            lines = [f"{w.day(i.day_of_week)}: {w.item(i, room_always=room_always)}" for i in items]
        if items:
            return ScheduleAnswer(w.lines(header, lines), OUTCOME_ANSWERED)
        if await _no_timetable(db, user, target, now):
            return ScheduleAnswer(w.t["no_timetable"], OUTCOME_NO_TIMETABLE)
        return ScheduleAnswer(w.t["subject_empty"].format(subject=subject_name), OUTCOME_ANSWERED)

    if intent == "class_named":
        blocks: list[str] = []
        names = {c.id: c.name for c in ctx.classes}
        any_item = False
        for class_id in _weekly_classes(decision, ctx):
            items = _ordered(await _weekly(db, user, [class_id], now))
            any_item = any_item or bool(items)
            klass = names.get(class_id, items[0].class_name if items else "")
            if items:
                blocks.append(w.lines(w.t["class_header"].format(klass=klass),
                                      [f"{w.day(i.day_of_week)}: {w.item(i)}" for i in items]))
            else:
                blocks.append(w.t["class_empty"].format(klass=klass))
        if not any_item:
            return ScheduleAnswer(w.t["no_timetable"], OUTCOME_NO_TIMETABLE)
        return ScheduleAnswer("\n".join(blocks), OUTCOME_ANSWERED)

    raise ValueError("not a schedule intent")
