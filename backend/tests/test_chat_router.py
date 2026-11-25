"""
tests/test_chat_router.py
────────────────────────
Step 6a-7 (plan 3, 11.1): the pure chat router. route(text, ctx) -> RouteDecision.

What is proved here:
  - schedule phrases (English, Marathi in Devanagari and Romanised) route to the right intent, with the right
    class, subject and day;
  - look-alikes ("next topic", "room for improvement", "class diagram in UML", ...) go to rag;
  - class and subject names are matched ONLY against what the caller is authorised for (difflib ratio >= 0.85);
    a name outside that scope behaves exactly like a name that does not exist;
  - two or more matches give clarify and list only authorised ids;
  - the router is total (any string gives a RouteDecision), bounded (500 characters) and pure (imports and calls
    are scanned in the source).
"""

import ast
import dataclasses
import difflib
import random
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.services import chat_router, chat_router_phrases
from app.services.chat_router import (
    DEFAULT_INDEX,
    INTENTS,
    MAX_INPUT_CHARS,
    NAME_MATCH_RATIO,
    RULE_IDS,
    NamedRef,
    RouteDecision,
    RouterContext,
    build_phrase_index,
    normalise,
    route,
)
from app.services.chat_router_phrases import NEXT, WHEN, LanguageTable, PhraseRule

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 25, 10, 15, tzinfo=IST)  # a Wednesday
TODAY = date(2026, 11, 25)
TOMORROW = date(2026, 11, 26)
DAYS = {  # the next date (today included) for each weekday, seen from Wednesday 2026-11-25
    "mon": date(2026, 11, 30),
    "tue": date(2026, 12, 1),
    "wed": date(2026, 11, 25),
    "thu": date(2026, 11, 26),
    "fri": date(2026, 11, 27),
    "sat": date(2026, 11, 28),
    "sun": date(2026, 11, 29),
}
NS = uuid.UUID("12345678-1234-5678-1234-567812345678")

SUBJECT_NAMES = (
    "Physics",
    "Chemistry",
    "Mathematics",
    "Computer Science",
    "History",
    "भौतिकशास्त्र",
    "रसायनशास्त्र",
    "गणित",
    "इतिहास",
)
CLASS_NAMES_BY_ROLE = {
    "student": ("10-A",),
    "teacher": ("10-A", "10-B"),
    "admin": ("10-A", "10-B", "10-C", "9-A"),
}


def ref(name: str, salt: str = "") -> NamedRef:
    return NamedRef(id=uuid.uuid5(NS, salt + name), name=name)


def make_ctx(
    role: str = "student",
    classes: tuple[str, ...] | None = None,
    subjects: tuple[str, ...] = SUBJECT_NAMES,
    now: datetime = NOW,
    tz: object = IST,
) -> RouterContext:
    names = CLASS_NAMES_BY_ROLE.get(role, ("10-A",)) if classes is None else classes
    return RouterContext(
        role=role,  # type: ignore[arg-type]
        classes=tuple(ref(n) for n in names),
        subjects=tuple(ref(n) for n in subjects),
        timezone=tz,  # type: ignore[arg-type]
        now=now,
    )


def check_invariants(d: RouteDecision, ctx: RouterContext, index=DEFAULT_INDEX) -> None:  # type: ignore[no-untyped-def]
    """What is true of EVERY decision, whatever the text."""
    assert isinstance(d, RouteDecision)
    assert d.route in ("schedule", "clarify", "rag")
    assert d.confidence == ("low" if d.route == "rag" else "high")
    assert isinstance(d.rule_ids, tuple) and len(d.rule_ids) == len(set(d.rule_ids))
    assert set(d.rule_ids) <= set(index.rule_ids), set(d.rule_ids) - set(index.rule_ids)
    class_ids = {c.id for c in ctx.classes}
    subject_ids = {s.id for s in ctx.subjects}
    assert set(d.class_ids or ()) <= class_ids, "a class outside the caller's scope"
    assert d.subject_id is None or d.subject_id in subject_ids, "a subject outside the caller's scope"
    assert set(d.subject_ids) <= subject_ids
    if d.route == "rag":
        assert d.intent is None and d.class_ids is None and d.subject_id is None
        assert d.day is None and d.subject_ids == ()
        # plain rag lists nothing; every other rag decision is a recorded miss
        assert d.rule_ids == () or any(r.startswith("miss.") for r in d.rule_ids)
    else:
        assert d.intent in INTENTS
        assert len(d.rule_ids) >= 1
        assert d.rule_ids[-1] == f"intent.{d.intent}"
    if d.route == "clarify":
        assert len(d.class_ids or ()) > 1 or len(d.subject_ids) > 1
    else:
        assert d.subject_ids == ()
    if d.day is not None:
        assert d.intent in ("today", "tomorrow", "weekday", "subject_when", "room")


@dataclass(frozen=True)
class Case:
    text: str
    intent: str
    subject: str | None = None
    classes: tuple[str, ...] | None = None  # labels of the expected class_ids (None = all the caller's classes)
    day: date | None = None
    role: str = "student"


def run_case(case: Case) -> RouteDecision:
    ctx = make_ctx(case.role)
    d = route(case.text, ctx)
    check_invariants(d, ctx)
    assert d.route == "schedule", f"{case.text!r} -> {d}"
    assert d.intent == case.intent, f"{case.text!r} -> {d}"
    assert d.confidence == "high"
    assert d.subject_id == (ref(case.subject).id if case.subject else None), f"{case.text!r} -> {d}"
    expected_classes = None if case.classes is None else tuple(ref(n).id for n in case.classes)
    assert d.class_ids == expected_classes, f"{case.text!r} -> {d}"
    assert d.day == case.day, f"{case.text!r} -> {d}"
    return d


def cases(intent: str, texts: list[str], **kw) -> list[Case]:  # type: ignore[no-untyped-def]
    return [Case(t, intent, **kw) for t in texts]


# ── English: at least 60 schedule phrases ─────────────────────────────────────

EN_CASES: list[Case] = [
    *cases(
        "next_lecture",
        [
            "next lecture",
            "What's my next lecture?",
            "what is my next class",
            "next period",
            "when is my next lecture",
            "what time is my next class",
            "which is the next lecture",
            "Can you tell me my next lecture please",
            "upcoming lecture",
            "my upcoming class",
            "show me the next period",
            "NEXT LECTURE???",
            "next    lecture",
            "next-lecture",
            "what is the next lecture sir",
            "hello what's my next class",
            "tell me next lecture pls",
            "do I have a next lecture",
            "what is my next session",
            "NEXT lecture, please.",
            "Next class?",
        ],
    ),
    *cases(
        "now",
        [
            "what's on now",
            "what is on now",
            "what is on right now",
            "which lecture is now",
            "current lecture",
            "what is my current class",
            "ongoing lecture",
            "current period",
            "which period is it now",
            "is there a class now",
            "do I have a lecture now",
            "lecture right now",
            "which lecture is on right now",
            "what class do i have at the moment",
            "currently which lecture",
            "What's on now?",
        ],
    ),
    *cases(
        "today",
        [
            "today's timetable",
            "todays timetable",
            "today timetable",
            "timetable for today",
            "what is today's schedule",
            "show today's lectures",
            "my lectures today",
            "what lectures do I have today",
            "do I have any class today",
            "is there a lecture today",
            "today's classes",
            "classes today",
            "schedule today",
            "give me today's schedule please",
            "TODAY'S TIMETABLE",
            "today’s timetable",
            "list today's periods",
            "what is the schedule for today",
            "what's the timetable for today",
        ],
        day=TODAY,
    ),
    *cases(
        "tomorrow",
        [
            "tomorrow's timetable",
            "tomorrows schedule",
            "timetable for tomorrow",
            "what lectures do i have tomorrow",
            "is there a class tomorrow",
            "tomorrow's lectures",
            "schedule for tomorrow please",
            "do we have lecture tomorrow",
            "what is tomorrow's schedule",
            "classes tomorrow",
            "tomorrow timetable",
            "will there be a lecture tomorrow",
        ],
        day=TOMORROW,
    ),
    Case("monday timetable", "weekday", day=DAYS["mon"]),
    Case("timetable for monday", "weekday", day=DAYS["mon"]),
    Case("monday's schedule", "weekday", day=DAYS["mon"]),
    Case("what lectures do I have on monday", "weekday", day=DAYS["mon"]),
    Case("tuesday's lectures", "weekday", day=DAYS["tue"]),
    Case("wednesday timetable", "weekday", day=DAYS["wed"]),  # today is Wednesday: the same date
    Case("thursday classes", "weekday", day=DAYS["thu"]),
    Case("friday schedule", "weekday", day=DAYS["fri"]),
    Case("schedule for friday", "weekday", day=DAYS["fri"]),
    Case("is there a class on saturday", "weekday", day=DAYS["sat"]),
    Case("sunday schedule", "weekday", day=DAYS["sun"]),
    Case("mon timetable", "weekday", day=DAYS["mon"]),
    Case("fri schedule", "weekday", day=DAYS["fri"]),
    Case("what is the timetable on friday", "weekday", day=DAYS["fri"]),
    Case("show me thursday's timetable", "weekday", day=DAYS["thu"]),
    # subject names (matched only against the caller's subjects)
    *cases(
        "subject_when",
        [
            "when is physics",
            "when is the physics lecture",
            "when do we have physics",
            "next physics lecture",
            "when is the next physics class",
            "what time is the physics class",
            "when's physics",
            "WHEN IS PHYSICS?",
        ],
        subject="Physics",
    ),
    Case("when is my chemistry class", "subject_when", subject="Chemistry"),
    Case("when is mathematics", "subject_when", subject="Mathematics"),
    Case("when is computer science", "subject_when", subject="Computer Science"),
    Case("when is my computer science lecture", "subject_when", subject="Computer Science"),
    Case("computer science class tomorrow", "subject_when", subject="Computer Science", day=TOMORROW),
    Case("physics lecture tomorrow", "subject_when", subject="Physics", day=TOMORROW),
    Case("chemistry timetable today", "subject_when", subject="Chemistry", day=TODAY),
    Case("when is history lecture today", "subject_when", subject="History", day=TODAY),
    Case("when is physics on monday", "subject_when", subject="Physics", day=DAYS["mon"]),
    *cases(
        "room",
        [
            "which room is physics",
            "physics room",
            "room for physics",
            "what is the room for physics",
            "where is the physics room",
            "physics lecture room",
            "which room is the physics lecture in",
        ],
        subject="Physics",
    ),
    Case("chemistry room", "room", subject="Chemistry"),
    Case("which room for computer science", "room", subject="Computer Science"),
    Case("room of history class", "room", subject="History"),
    Case("which room is history in", "room", subject="History"),
    Case("mathematics classroom tomorrow", "room", subject="Mathematics", day=TOMORROW),
]

# ── Marathi: Devanagari ───────────────────────────────────────────────────────

MR_DEVANAGARI_CASES: list[Case] = [
    *cases(
        "next_lecture",
        [
            "पुढचा तास कधी आहे",
            "पुढचे लेक्चर",
            "माझे पुढचे लेक्चर कोणते आहे",
            "पुढचा तास",
            "पुढची तासिका कोणती",
            "पुढील लेक्चर कधी",
            "माझा पुढचा तास कोणता",
            "पुढचा वर्ग कधी आहे",
            "मला पुढचे लेक्चर सांगा",
            "पुढचा तास कोणता आहे?",
            "पुढचे लेक्चर कधी आहे?",
            "पुढचा पिरियड कधी",
            "पुढचा क्लास कोणता",
        ],
    ),
    *cases(
        "now",
        [
            "आता कोणता तास आहे",
            "आता कोणते लेक्चर आहे",
            "सध्या कोणता तास चालू आहे",
            "आता लेक्चर आहे का",
            "आत्ता कोणता तास आहे",
            "आताचा तास कोणता",
            "चालू लेक्चर कोणते",
            "आता कोणता क्लास आहे",
            "आता काय आहे",
            "आता काय चालू आहे",
        ],
    ),
    *cases(
        "today",
        [
            "आजचे वेळापत्रक",
            "आजचे लेक्चर",
            "आज कोणते तास आहेत",
            "आजचे टाइमटेबल",
            "आजचे तास",
            "आज वेळापत्रक सांगा",
            "आज कोणते लेक्चर आहेत",
            "आज लेक्चर आहे का",
            "आज क्लास आहे का",
            "आजचे वेळापत्रक दाखवा",
            "मला आजचे वेळापत्रक सांगा",
            "आजचं वेळापत्रक",
        ],
        day=TODAY,
    ),
    *cases(
        "tomorrow",
        [
            "उद्याचे वेळापत्रक",
            "उद्या कोणते तास आहेत",
            "उद्या लेक्चर आहे का",
            "उद्याचे लेक्चर",
            "उद्याचे टाइमटेबल",
            "उद्याचे तास",
            "उद्या क्लास आहे का",
            "उद्याचे वेळापत्रक सांगा",
        ],
        day=TOMORROW,
    ),
    Case("सोमवारचे वेळापत्रक", "weekday", day=DAYS["mon"]),
    Case("सोमवारी कोणते तास आहेत", "weekday", day=DAYS["mon"]),
    Case("मंगळवारचे लेक्चर", "weekday", day=DAYS["tue"]),
    Case("बुधवारचे वेळापत्रक", "weekday", day=DAYS["wed"]),
    Case("गुरुवारचे वेळापत्रक", "weekday", day=DAYS["thu"]),
    Case("शुक्रवारचे वेळापत्रक", "weekday", day=DAYS["fri"]),
    Case("शनिवारी लेक्चर आहे का", "weekday", day=DAYS["sat"]),
    Case("रविवारचे वेळापत्रक", "weekday", day=DAYS["sun"]),
    Case("सोमवारचे टाइमटेबल", "weekday", day=DAYS["mon"]),
    Case("भौतिकशास्त्र कधी आहे", "subject_when", subject="भौतिकशास्त्र"),
    Case("भौतिकशास्त्र कधी", "subject_when", subject="भौतिकशास्त्र"),
    Case("माझे भौतिकशास्त्र लेक्चर कधी आहे", "subject_when", subject="भौतिकशास्त्र"),
    Case("भौतिकशास्त्र लेक्चर उद्या", "subject_when", subject="भौतिकशास्त्र", day=TOMORROW),
    Case("रसायनशास्त्र कधी आहे", "subject_when", subject="रसायनशास्त्र"),
    Case("रसायनशास्त्र तास आज", "subject_when", subject="रसायनशास्त्र", day=TODAY),
    Case("गणित कधी आहे", "subject_when", subject="गणित"),
    Case("गणित लेक्चर कधी आहे", "subject_when", subject="गणित"),
    Case("गणित तास कधी आहे", "subject_when", subject="गणित"),
    Case("पुढचे गणित लेक्चर", "subject_when", subject="गणित"),
    Case("मला गणित केव्हा आहे", "subject_when", subject="गणित"),
    Case("इतिहास कधी आहे", "subject_when", subject="इतिहास"),
    Case("गणित वर्ग उद्या", "subject_when", subject="गणित", day=TOMORROW),
    Case("भौतिकशास्त्र कोणत्या खोलीत आहे", "room", subject="भौतिकशास्त्र"),
    Case("गणित कोणत्या खोलीत", "room", subject="गणित"),
    Case("रसायनशास्त्र खोली", "room", subject="रसायनशास्त्र"),
    Case("कोणत्या खोलीत गणित", "room", subject="गणित"),
    Case("इतिहास रूम", "room", subject="इतिहास"),
    Case("गणित खोली कोणती", "room", subject="गणित"),
    Case("भौतिकशास्त्र कोणत्या रूममध्ये", "room", subject="भौतिकशास्त्र"),
    Case("भौतिकशास्त्र वर्गखोली", "room", subject="भौतिकशास्त्र"),
]

# ── Marathi: Romanised (treated as Latin text) ────────────────────────────────

MR_ROMAN_CASES: list[Case] = [
    *cases(
        "next_lecture",
        [
            "maza next lecture kadhi aahe",
            "majha next lecture kadhi ahe",
            "pudhcha lecture kadhi",
            "pudhcha tas kadhi aahe",
            "pudhcha tas konta",
            "maze pudhche lecture konte",
            "next lecture kadhi aahe",
            "next class kadhi",
            "pudhcha class kadhi aahe",
            "mala pudhcha lecture sang",
            "pudhcha period konta aahe",
            "next tasika kadhi",
        ],
    ),
    *cases(
        "now",
        [
            "aata konta tas aahe",
            "aata kon lecture aahe",
            "sadhya konta tas chalu aahe",
            "aata lecture aahe ka",
            "aata class aahe ka",
            "aatta konta tas aahe",
            "current lecture konta",
            "aata kay aahe",
        ],
    ),
    *cases(
        "today",
        [
            "aajche velapatrak",
            "aajcha timetable",
            "aaj konte tas aahet",
            "aaj lecture aahe ka",
            "aaj class aahe ka",
            "aajche lecture",
            "aajche velapatrak sang",
            "aajche timetable dakhva",
            "aaj kay lecture aahe",
        ],
        day=TODAY,
    ),
    *cases(
        "tomorrow",
        [
            "udyache velapatrak",
            "udya konte tas aahet",
            "udya lecture aahe ka",
            "udyache lecture",
            "udyacha timetable",
            "udya class aahe ka",
            "udyache velapatrak sang",
        ],
        day=TOMORROW,
    ),
    Case("somvarche velapatrak", "weekday", day=DAYS["mon"]),
    Case("somvari konte tas aahet", "weekday", day=DAYS["mon"]),
    Case("mangalvarche timetable", "weekday", day=DAYS["tue"]),
    Case("budhvarche velapatrak", "weekday", day=DAYS["wed"]),
    Case("guruvarche velapatrak", "weekday", day=DAYS["thu"]),
    Case("shukravarche velapatrak", "weekday", day=DAYS["fri"]),
    Case("shanivari lecture aahe ka", "weekday", day=DAYS["sat"]),
    Case("ravivar velapatrak", "weekday", day=DAYS["sun"]),
    Case("physics kadhi aahe", "subject_when", subject="Physics"),
    Case("physics lecture kadhi aahe", "subject_when", subject="Physics"),
    Case("chemistry class kadhi", "subject_when", subject="Chemistry"),
    Case("mathematics kadhi aahe", "subject_when", subject="Mathematics"),
    Case("maza physics lecture kadhi aahe", "subject_when", subject="Physics"),
    Case("history kadhi aahe", "subject_when", subject="History"),
    Case("computer science kadhi aahe", "subject_when", subject="Computer Science"),
    Case("physics lecture udya", "subject_when", subject="Physics", day=TOMORROW),
    Case("pudhcha physics lecture kadhi", "subject_when", subject="Physics"),
    Case("physics kontya kholit aahe", "room", subject="Physics"),
    Case("physics room konti aahe", "room", subject="Physics"),
    Case("chemistry kholi konti", "room", subject="Chemistry"),
    Case("mathematics kontya kholit", "room", subject="Mathematics"),
    Case("history kholi", "room", subject="History"),
]
MR_CASES = MR_DEVANAGARI_CASES + MR_ROMAN_CASES


def test_the_tables_hold_at_least_60_schedule_phrases_per_language() -> None:
    assert len(EN_CASES) >= 60
    assert len(MR_CASES) >= 60
    assert len(MR_DEVANAGARI_CASES) >= 30 and len(MR_ROMAN_CASES) >= 30
    assert len({c.text for c in EN_CASES}) == len(EN_CASES)
    assert len({c.text for c in MR_CASES}) == len(MR_CASES)


@pytest.mark.parametrize("case", EN_CASES, ids=lambda c: c.text)
def test_english_schedule_phrase_routes(case: Case) -> None:
    run_case(case)


@pytest.mark.parametrize("case", MR_DEVANAGARI_CASES, ids=lambda c: c.text)
def test_marathi_devanagari_schedule_phrase_routes(case: Case) -> None:
    run_case(case)


@pytest.mark.parametrize("case", MR_ROMAN_CASES, ids=lambda c: c.text)
def test_romanised_marathi_schedule_phrase_routes(case: Case) -> None:
    run_case(case)


# ── look-alikes that must go to rag ───────────────────────────────────────────

# Plain rag: no intent rule fired at all, so no rule id is listed.
EN_PLAIN_RAG = [
    "What is the next topic in physics",
    "when was the exam of",
    "room for improvement",
    "class diagram in UML",
    "next topic",
    "what is a lecture",
    "what is the definition of a period in physics",
    "what is the time period of a pendulum",
    "the next step in the experiment",
    "tomorrow",
    "today",
    "now",
    "next",
    "monday",
    "physics today",
    "next physics",
    "lecture",
    "timetable",
    "schedule",
    "when is the exam",
    "what is the syllabus for physics",
    "who teaches physics",
    "explain newton's second law",
    "room temperature in celsius",
    "what is a class in python",
    "today's homework",
    "what happens in the next chapter of physics",
    "I missed the lecture",
    "when will the results come",
    "physics lecture",
    "class room",
    "schedule a meeting with the principal",
    "how do I schedule a task in linux",
    "class timetable generation algorithm",
]
# Schedule-like but not answerable by rule: rag with the rules that fired and a miss id recorded.
EN_MISS_RAG = [
    "when was the exam of physics",
    "what is the next class in the inheritance hierarchy",
    "explain today's lecture on thermodynamics",
    "summarise today's lecture",
    "next lecture notes",
    "next lecture please explain recursion",
    "when is the physics exam",
    "tomorrow's exam schedule",
    "I missed the lecture today",
    "what was taught in today's lecture",
    "today's lecture was boring",
    "what is the next lecture about",
    "next lecture of the semester ends in december",
]
MR_PLAIN_RAG = [
    "पुढचा विषय कोणता",
    "भौतिकशास्त्र म्हणजे काय",
    "परीक्षा कधी आहे",
    "वर्ग आकृती म्हणजे काय",
    "आजचा गृहपाठ",
    "उद्याची परीक्षा",
    "वेळापत्रक म्हणजे काय",
    "तास म्हणजे काय",
    "आता काय करावे",
    "सोमवार",
    "खोली स्वच्छ करा",
    "उद्या",
    "आता",
    "पुढचे",
    "pudhcha vishay konta",
    "pariksha kadhi aahe",
    "aajcha gruhpath",
    "udyachi pariksha",
    "velapatrak mhanje kay",
    "class diagram mhanje kay",
    "room temperature kiti aahe",
    "aaj",
    "somvar",
    "तास",
    "आजच्या लेक्चरचा सारांश",  # the inflected "लेक्चरचा" is not a schedule noun
]
MR_MISS_RAG = [
    "भौतिकशास्त्र परीक्षा कधी आहे",
    "आजचे लेक्चर समजावून सांगा",
    "पुढचे लेक्चर नोट्स",
    "उद्याचे वेळापत्रक बदला",
    "physics pariksha kadhi aahe",
    "pudhcha lecture notes dya",
    "aajchya lecture cha saransh",
    "aajche lecture samjavun sang",
    "udyache velapatrak badla",
]
HINDI_PHRASES = ["अगला लेक्चर कब है", "अभी कौन सा लेक्चर है"]  # no Hindi table yet (owner question Q5)


def test_the_tables_hold_at_least_30_lookalikes_per_language() -> None:
    assert len(EN_PLAIN_RAG) + len(EN_MISS_RAG) >= 30
    assert len(MR_PLAIN_RAG) + len(MR_MISS_RAG) >= 30
    for name in ("room for improvement", "class diagram in UML", "next topic", "when was the exam of"):
        assert name in EN_PLAIN_RAG
    assert "What is the next topic in physics" in EN_PLAIN_RAG


@pytest.mark.parametrize("text", EN_PLAIN_RAG + MR_PLAIN_RAG)
def test_plain_lookalike_goes_to_rag_with_no_rule(text: str) -> None:
    ctx = make_ctx()
    d = route(text, ctx)
    check_invariants(d, ctx)
    assert d.route == "rag", d
    assert d.rule_ids == (), d


@pytest.mark.parametrize("text", EN_MISS_RAG + MR_MISS_RAG)
def test_schedule_like_lookalike_goes_to_rag_and_records_the_miss(text: str) -> None:
    ctx = make_ctx()
    d = route(text, ctx)
    check_invariants(d, ctx)
    assert d.route == "rag", d
    assert d.rule_ids and any(r.startswith("miss.") for r in d.rule_ids), d


def test_the_plan_example_next_topic_in_physics_goes_to_rag() -> None:
    d = route("What is the next topic in physics", make_ctx())
    assert d.route == "rag" and d.intent is None and d.subject_id is None


@pytest.mark.parametrize("text", HINDI_PHRASES)
def test_hindi_has_no_phrase_table_yet_so_it_goes_to_rag(text: str) -> None:
    # Q5 is pending: English and Marathi only. When Hindi is approved this test moves to the schedule tables.
    d = route(text, make_ctx())
    assert d.route == "rag"


def test_a_language_is_added_by_adding_a_table() -> None:
    hindi = LanguageTable(
        code="hi",
        rules=(
            PhraseRule("hi.order.next", NEXT, ("अगला",)),
            PhraseRule("hi.when.when", WHEN, ("कब",)),
        ),
        fillers=("है",),
    )
    ctx = make_ctx()
    text = "अगला लेक्चर कब है"
    assert route(text, ctx).route == "rag"  # the shipped tables have no Hindi
    index = build_phrase_index((*chat_router_phrases.LANGUAGE_TABLES, hindi))
    d = route(text, ctx, index=index)
    check_invariants(d, ctx, index)
    assert (d.route, d.intent) == ("schedule", "next_lecture")
    assert "hi.order.next" in d.rule_ids and "hi.when.when" in d.rule_ids


def test_the_shipped_tables_are_english_and_marathi_only() -> None:
    assert [t.code for t in chat_router_phrases.LANGUAGE_TABLES] == ["en", "mr"]


# ── role cases ────────────────────────────────────────────────────────────────


def test_student_with_one_class_gets_all_classes_by_default() -> None:
    ctx = make_ctx("student", classes=("10-A",))
    d = route("what is my next lecture", ctx)
    assert (d.route, d.intent, d.class_ids) == ("schedule", "next_lecture", None)


def test_student_with_several_classes_defaults_to_all_of_them() -> None:
    ctx = make_ctx("student", classes=("10-A", "10-C"))
    for text, intent in [
        ("next lecture", "next_lecture"),
        ("today's timetable", "today"),
        ("what is on now", "now"),
    ]:
        d = route(text, ctx)
        check_invariants(d, ctx)
        assert (d.route, d.intent, d.class_ids) == ("schedule", intent, None), text


def test_student_with_several_classes_can_name_one_of_theirs() -> None:
    ctx = make_ctx("student", classes=("10-A", "10-C"))
    d = route("next lecture for 10-C", ctx)
    assert (d.route, d.intent, d.class_ids) == ("schedule", "next_lecture", (ref("10-C").id,))
    d = route("tomorrow's timetable for 10 a", ctx)
    assert (d.intent, d.class_ids, d.day) == ("tomorrow", (ref("10-A").id,), TOMORROW)


def test_student_cannot_use_the_class_named_intent() -> None:
    ctx = make_ctx("student", classes=("10-A", "10-C"))
    d = route("timetable for 10-A", ctx)
    check_invariants(d, ctx)
    assert d.route == "rag" and "miss.role_not_allowed" in d.rule_ids
    d = route("timetable for all classes", ctx)
    assert d.route == "rag" and "miss.role_not_allowed" in d.rule_ids


def test_a_role_the_router_does_not_know_is_treated_like_a_student() -> None:
    ctx = make_ctx("root", classes=("10-A",))
    d = route("timetable for 10-A", ctx)
    check_invariants(d, ctx)
    assert d.route == "rag" and "miss.role_not_allowed" in d.rule_ids


@pytest.mark.parametrize("role", ["teacher", "admin"])
def test_teacher_and_admin_get_the_class_named_intent(role: str) -> None:
    ctx = make_ctx(role)
    for text in ["timetable for 10-A", "10-A timetable", "schedule of 10 a", "show timetable for class 10-A", "10a schedule"]:
        d = route(text, ctx)
        check_invariants(d, ctx)
        assert (d.route, d.intent, d.class_ids) == ("schedule", "class_named", (ref("10-A").id,)), text
    for text in ["timetable for all classes", "all classes timetable", "every class schedule", "सर्व वर्गांचे वेळापत्रक"]:
        d = route(text, ctx)
        check_invariants(d, ctx)
        assert (d.route, d.intent, d.class_ids) == ("schedule", "class_named", None), text


def test_teacher_next_lecture_without_a_class_is_all_assigned_classes() -> None:
    ctx = make_ctx("teacher")
    d = route("what is my next lecture", ctx)
    assert (d.intent, d.class_ids) == ("next_lecture", None)
    d = route("next lecture for 10-B", ctx)
    assert (d.intent, d.class_ids) == ("next_lecture", (ref("10-B").id,))


def test_admin_with_one_class_and_with_all_classes() -> None:
    one = make_ctx("admin", classes=("10-A",))
    d = route("timetable for 10-A", one)
    assert (d.intent, d.class_ids) == ("class_named", (ref("10-A").id,))
    d = route("timetable for 10-B", one)  # a class that is not in this admin's scope does not exist for the router
    assert d.route == "rag"
    full = make_ctx("admin")
    for label in ("10-A", "10-B", "10-C", "9-A"):
        d = route(f"next lecture for {label}", full)
        assert (d.intent, d.class_ids) == ("next_lecture", (ref(label).id,)), label
    d = route("next lecture for all classes", full)
    assert (d.intent, d.class_ids) == ("next_lecture", None)
    d = route("what is on now for 10-C", full)
    assert (d.intent, d.class_ids) == ("now", (ref("10-C").id,))
    d = route("10-B today's timetable", full)
    assert (d.intent, d.class_ids, d.day) == ("today", (ref("10-B").id,), TODAY)


def test_class_spelling_variants_match_the_same_class() -> None:
    ctx = make_ctx("admin")
    for text in ["next lecture 10-A", "next lecture 10 A", "next lecture 10a", "next lecture 10A", "next lecture १०-A"]:
        d = route(text, ctx)
        assert d.class_ids == (ref("10-A").id,), text


# ── clarify ───────────────────────────────────────────────────────────────────


def test_two_classes_named_in_the_text_give_clarify_with_both_ids() -> None:
    ctx = make_ctx("admin")
    d = route("next lecture for 10-A and 10-B", ctx)
    check_invariants(d, ctx)
    assert (d.route, d.intent, d.confidence) == ("clarify", "next_lecture", "high")
    assert d.class_ids == (ref("10-A").id, ref("10-B").id)
    assert d.subject_id is None


def test_two_classes_with_the_same_name_give_clarify() -> None:
    first, second = ref("10-A", "year-1:"), ref("10-A", "year-2:")
    ctx = dataclasses.replace(make_ctx("admin"), classes=(first, second, ref("10-B")))
    d = route("today's timetable for 10-A", ctx)
    check_invariants(d, ctx)
    assert d.route == "clarify" and set(d.class_ids or ()) == {first.id, second.id}
    assert ref("10-B").id not in (d.class_ids or ())


def test_two_subjects_named_give_clarify_with_both_subject_ids() -> None:
    ctx = make_ctx()
    d = route("when is physics or chemistry", ctx)
    check_invariants(d, ctx)
    assert (d.route, d.intent) == ("clarify", "subject_when")
    assert d.subject_id is None
    assert d.subject_ids == (ref("Physics").id, ref("Chemistry").id)


def test_a_near_name_that_matches_two_subjects_gives_clarify() -> None:
    # "physic" is close to both "Physics" (0.92) and "Physical" (0.857): ambiguous, so clarify
    ctx = make_ctx(subjects=("Physics", "Physical"))
    d = route("when is physic", ctx)
    assert d.route == "clarify" and set(d.subject_ids) == {ref("Physics").id, ref("Physical").id}


def test_an_exact_match_beats_a_fuzzy_neighbour() -> None:
    ctx = make_ctx(subjects=("Physics", "Physics 1"))
    d = route("when is physics", ctx)
    assert d.route == "schedule" and d.subject_id == ref("Physics").id
    d = route("when is physics 1", ctx)
    assert d.route == "schedule" and d.subject_id == ref("Physics 1").id


def test_an_exact_match_is_not_made_ambiguous_by_a_close_name() -> None:
    # "Physics 1" and "Physics 2" are 0.89 alike: typing one of them exactly must not ask "which one?"
    ctx = make_ctx(subjects=("Physics 1", "Physics 2"))
    assert ratio("physics 1", "physics 2") >= NAME_MATCH_RATIO
    d = route("when is physics 1", ctx)
    assert (d.route, d.subject_id, d.subject_ids) == ("schedule", ref("Physics 1").id, ())
    d = route("when is physics 2", ctx)
    assert (d.route, d.subject_id) == ("schedule", ref("Physics 2").id)
    # a typo close to both is a real ambiguity
    d = route("when is physics 3", ctx)
    assert d.route == "clarify" and set(d.subject_ids) == {ref("Physics 1").id, ref("Physics 2").id}


def test_a_named_class_wins_over_all_classes() -> None:
    d = route("next lecture for all classes and 10-A", make_ctx("admin"))
    assert (d.intent, d.class_ids) == ("next_lecture", (ref("10-A").id,))


def test_names_are_only_looked_at_when_a_schedule_word_is_there(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = chat_router._find_names

    def spy(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(chat_router, "_find_names", spy)
    ctx = make_ctx("admin")
    for text in ["what is photosynthesis", "physics 10-A", "explain the heat equation in physics", ""]:
        assert route(text, ctx).route == "rag"
    assert calls == []
    assert route("when is physics", ctx).route == "schedule"
    assert calls == [1]


def test_clarify_lists_only_authorised_names() -> None:
    ctx = make_ctx("admin", classes=("10-A", "10-B"))
    d = route("next lecture for 10-A and 10-B and 10-C", ctx)  # 10-C is not in scope: it is an unexplained token
    check_invariants(d, ctx)
    assert d.route == "rag"
    d = route("next lecture for 10-A and 10-B", ctx)
    assert d.route == "clarify" and set(d.class_ids or ()) == {ref("10-A").id, ref("10-B").id}


# ── names only from the caller's scope ────────────────────────────────────────

UNAUTHORISED = ["Biology", "Geography", "11-Q", "Grade 12 Commerce", "भूगोल", "Sanskrit", "9-Z"]
PROBE_SHAPES = [
    "when is {x}",
    "{x} room",
    "which room is {x}",
    "next lecture for {x}",
    "timetable for {x}",
    "{x} lecture tomorrow",
    "{x} kadhi aahe",
    "{x} कधी आहे",
    "what is on now for {x}",
    "when is my {x} class",
    "today's schedule {x}",
    "{x} lecture",
]


def scramble(name: str, rng: random.Random) -> str:
    """A name of the same shape (same length, same kinds of characters) that is not any real name."""
    out = []
    for ch in name:
        if ch.isdigit():
            out.append(rng.choice("2357"))
        elif ch.isascii() and ch.isalpha():
            out.append(rng.choice("qzxjkwv"))
        elif "ऀ" <= ch <= "ॿ" and ch.isalpha():
            out.append(rng.choice("ऩऴऱळ"))
        else:
            out.append(ch)
    return "".join(out)


@pytest.mark.parametrize("role", ["student", "teacher", "admin"])
def test_a_probe_for_a_name_outside_the_scope_equals_random_text_of_the_same_shape(role: str) -> None:
    ctx = make_ctx(role, subjects=("Physics", "Chemistry", "भौतिकशास्त्र"))
    rng = random.Random(7)
    for name in UNAUTHORISED:
        for shape in PROBE_SHAPES:
            probe = route(shape.format(x=name), ctx)
            control = route(shape.format(x=scramble(name, rng)), ctx)
            check_invariants(probe, ctx)
            assert probe == control, (shape, name, probe, control)


def test_the_same_probe_matches_when_the_caller_is_authorised() -> None:
    # the control for the test above: the probes are not inert, authorisation is what changes the answer
    narrow = make_ctx(subjects=("Physics",), classes=("10-A",), role="admin")
    wide = make_ctx(subjects=("Physics", "Biology"), classes=("10-A", "10-B"), role="admin")
    assert route("when is biology", narrow).route == "rag"
    d = route("when is biology", wide)
    assert (d.route, d.intent, d.subject_id) == ("schedule", "subject_when", ref("Biology").id)
    assert route("timetable for 10-B", narrow).route == "rag"
    d = route("timetable for 10-B", wide)
    assert (d.intent, d.class_ids) == ("class_named", (ref("10-B").id,))


def test_a_decision_never_holds_an_id_outside_the_context() -> None:
    ctx = make_ctx("student", classes=("10-A",), subjects=("Physics",))
    for text in ["when is biology", "next lecture for 10-B", "physics and biology", "10-A and 10-B"]:
        check_invariants(route(text, ctx), ctx)


def test_an_unauthorised_name_in_a_clear_intent_goes_to_rag_like_a_nonexistent_one() -> None:
    ctx = make_ctx("student")
    a = route("next lecture for biology club", ctx)
    b = route("next lecture for qzxjkwv zxjq", ctx)
    assert a.route == b.route == "rag" and a.rule_ids == b.rule_ids == (
        "en.order.next",
        "en.noun.lecture",
        "miss.unexplained_tokens",
    )


def test_the_router_works_on_the_names_in_the_context_only() -> None:
    # there is no other source of names: with no subjects and no classes nothing can match
    ctx = make_ctx("admin", classes=(), subjects=())
    for text in ["when is physics", "timetable for 10-A", "physics room"]:
        assert route(text, ctx).route == "rag"
    assert route("next lecture", ctx).intent == "next_lecture"


# ── fuzzy matching: the 0.85 border ───────────────────────────────────────────


def ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def test_the_ratio_limit_is_0_85() -> None:
    assert NAME_MATCH_RATIO == 0.85


def test_typo_at_exactly_the_border_matches() -> None:
    name, typed = "introduction physics", "iqzxoduction physics"
    assert ratio(typed, name) == 0.85  # 3 of 20 characters wrong: exactly the border
    ctx = make_ctx(subjects=(name,))
    d = route(f"when is {typed}", ctx)
    assert (d.route, d.subject_id) == ("schedule", ref(name).id)


def test_typo_just_below_the_border_does_not_match() -> None:
    name, typed = "introduction physics", "iqzxjduction physics"
    assert ratio(typed, name) < 0.85
    ctx = make_ctx(subjects=(name,))
    assert route(f"when is {typed}", ctx).route == "rag"
    name2, typed2 = "introduction to physics", "introduction to fisiks"
    assert 0.84 < ratio(typed2, name2) < 0.85
    ctx2 = make_ctx(subjects=(name2,))
    assert route(f"when is {typed2}", ctx2).route == "rag"
    assert route("when is introduction to phisics", ctx2).subject_id == ref(name2).id  # 0.957: a real typo


@pytest.mark.parametrize(
    ("typed", "name", "matches"),
    [
        ("chemistery", "Chemistry", True),
        ("chemstry", "Chemistry", True),
        ("mathematcs", "Mathematics", True),
        ("histroy", "History", True),  # 0.857, just above
        ("phisics", "Physics", True),
        ("computr science", "Computer Science", True),
        ("physcs", "Physics", True),  # 0.923
        ("fizix", "Physics", False),
        ("chem", "Chemistry", False),
        ("math", "Mathematics", False),
        ("hist", "History", False),
        ("comp sci", "Computer Science", False),
    ],
)
def test_subject_typos(typed: str, name: str, matches: bool) -> None:
    assert (ratio(typed, name.casefold()) >= NAME_MATCH_RATIO) == matches  # the table agrees with difflib
    d = route(f"when is {typed}", make_ctx())
    assert (d.route == "schedule" and d.subject_id == ref(name).id) == matches
    if not matches:
        assert d.route == "rag"


def test_class_names_are_matched_the_same_way() -> None:
    ctx = make_ctx("admin", classes=("Grade 10 Science", "Grade 10 Commerce"))
    d = route("timetable for grade 10 sciense", ctx)  # a typo, ratio well above 0.85
    assert (d.intent, d.class_ids) == ("class_named", (ref("Grade 10 Science").id,))
    d = route("timetable for grade 10", ctx)  # too short a part of both names
    assert d.route == "rag"


def test_a_name_made_only_of_ordinary_words_is_never_matched() -> None:
    # "A", "Today", "next lecture": matching them would swallow ordinary words of the question
    ctx = dataclasses.replace(make_ctx("admin"), classes=(ref("A"), ref("Today"), ref("next lecture"), ref("10-A")))
    d = route("is there a lecture today", ctx)
    assert d.intent == "today" and d.class_ids is None
    d = route("next lecture", ctx)
    assert d.intent == "next_lecture" and d.class_ids is None


# ── normalisation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Next  Lecture", "next lecture"),
        ("ＮＥＸＴ　ＬＥＣＴＵＲＥ", "next lecture"),  # full-width letters and ideographic space (NFKC)
        ("STRASSE", "strasse"),
        ("Straße", "strasse"),  # case fold
        ("ne​xt lec‍ture", "next lecture"),  # zero-width characters dropped (ingest clean_text rules)
        ("‮next‬ lecture", "next lecture"),  # bidi override dropped
        ("next\x00 lecture\x07", "next lecture"),  # control characters
        ("today's timetable", "todays timetable"),
        ("today’s timetable", "todays timetable"),
        ("next,lecture?!", "next lecture"),
        ("next_lecture/class", "next lecture class"),
        ("  next \t\n lecture  ", "next lecture"),
        ("१०-अ", "10 अ"),  # Devanagari digits to ASCII, hyphen to a space
        ("10अ", "10 अ"),
        ("10-A", "10 a"),
        ("10A", "10 a"),
        ("class10a", "class 10 a"),
        ("आजचे वेळापत्रक।", "आजचे वेळापत्रक"),  # the danda is punctuation
        ("", ""),
        ("!!!???", ""),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


def test_normalise_is_idempotent() -> None:
    rng = random.Random(3)
    for _ in range(500):
        text = "".join(rng.choice("abC 1-०९अआ​.,'’ ſß") for _ in range(rng.randint(0, 30)))
        once = normalise(text)
        assert normalise(once) == once


def test_invisible_characters_cannot_hide_a_schedule_phrase() -> None:
    ctx = make_ctx()
    d = route("n​e​x​t lec⁠ture", ctx)
    assert d.intent == "next_lecture"
    d = route("‮next lecture‬", ctx)
    assert d.intent == "next_lecture"


# ── input bound ───────────────────────────────────────────────────────────────


def test_the_input_bound_is_500_characters() -> None:
    assert MAX_INPUT_CHARS == 500


def test_text_over_500_characters_is_cut_before_matching() -> None:
    ctx = make_ctx()
    # without the cut the word "explain" would make this a miss; with it the router sees only "next lecture"
    text = "next lecture" + " " * 600 + "explain"
    assert len(text) > 500
    assert route(text, ctx).intent == "next_lecture"
    # the same text under the bound keeps the trailing word and is a miss
    short = "next lecture" + " " * 100 + "explain"
    assert route(short, ctx).route == "rag"


def test_the_cut_is_at_exactly_500_characters() -> None:
    ctx = make_ctx()
    phrase = "next lecture"
    fits = "the " * 122 + phrase  # 488 + 12 = exactly 500
    assert len(fits) == 500
    assert route(fits, ctx).intent == "next_lecture"
    pushed = "the " * 123 + phrase  # 504: the cut leaves "next lec", which is not a schedule noun
    assert len(pushed) == 504
    assert route(pushed, ctx).route == "rag"
    # nothing after character 500 is ever read
    assert route(fits + " explain", ctx).intent == "next_lecture"


# ── totality and robustness ───────────────────────────────────────────────────


def hostile_context(role: str) -> RouterContext:
    names = ["", "   ", "a", "Today", "next lecture", "𝒫hysics", "x" * 400, "10-A", "10-A", "!!!", "भौतिकशास्त्र", "​", "10 a"]
    return RouterContext(
        role=role,  # type: ignore[arg-type]
        classes=tuple(NamedRef(uuid.uuid5(NS, f"c{i}{n}"), n) for i, n in enumerate(names)),
        subjects=tuple(NamedRef(uuid.uuid5(NS, f"s{i}{n}"), n) for i, n in enumerate(reversed(names))),
        timezone=IST,
        now=NOW,
    )


def generated_strings(seed: int = 20261125) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = []
    ranges = [(0x20, 0x7E), (0x00, 0xFF), (0x0900, 0x097F), (0x0000, 0xFFFF), (0xD800, 0xDFFF), (0x10000, 0x10FFFF)]

    def char(lo: int, hi: int) -> str:
        return chr(rng.randint(lo, hi))

    for _ in range(900):  # random Unicode
        lo, hi = rng.choice(ranges)
        out.append("".join(char(lo, hi) for _ in range(rng.choice([0, 1, 2, 3, 8, 30, 120]))))
    for _ in range(40):  # very long
        lo, hi = rng.choice(ranges)
        out.append("".join(char(lo, hi) for _ in range(rng.randint(600, 6000))))
    controls = "".join(chr(c) for c in range(0, 32)) + "\x7f\x80\x85\x9f"
    bidi = "‪‫‬‭‮⁦⁧⁨⁩​‌‍‎‏⁠﻿"
    for _ in range(300):  # control and bidi characters, alone and glued into words
        words = [rng.choice(["next", "lecture", "today", "physics", "10-A", "आता", "तास"]) for _ in range(rng.randint(0, 6))]
        glue = "".join(rng.choice(controls + bidi) for _ in range(rng.randint(0, 3)))
        out.append(glue.join(words) + "".join(rng.choice(controls + bidi) for _ in range(rng.randint(0, 5))))
    punct = string.punctuation + "।॥…—–“”‘’«»¿¡§¶†‡•"
    for _ in range(100):
        out.append("".join(rng.choice(punct) for _ in range(rng.randint(0, 60))))
    out += ["", " ", "\t\n", "​", "   " * 200]
    for _ in range(80):  # only digits (ASCII, Devanagari, Arabic-Indic)
        digits = rng.choice(["0123456789", "०१२३४५६७८९", "٠١٢٣٤٥٦٧٨٩"])
        out.append("".join(rng.choice(digits) for _ in range(rng.randint(1, 80))))
    vocab = sorted({" ".join(p) for p in DEFAULT_INDEX.rules} | set(DEFAULT_INDEX.fillers))
    pieces = vocab + list(SUBJECT_NAMES) + ["10-A", "10 b", "all classes", "xqz", "123", "physic", "chemstry", "and", "or"]
    seps = [" ", " ", " ", "-", ",", "​", "  ", "\n"]
    for _ in range(1600):  # vocabulary soup, to reach the matching code and not only the early exits
        n = rng.randint(1, 25)
        out.append("".join(rng.choice(pieces) + rng.choice(seps) for _ in range(n)))
    # adversarial 500-character inputs: repeated characters and near matches
    for ch in ["a", "आ", "1", " ", "-", "​", "ऺ", "x"]:
        out.append(ch * 500)
        out.append(ch * 501)
    for unit in ["next ", "next lecture ", "phyisics ", "chemstry ", "10-A ", "all classes ", "when is ", "the ", "आता तास ", "10 ", "a "]:
        out.append((unit * 600)[:500])
        out.append((unit * 600)[:499])
    for name in SUBJECT_NAMES:
        near = name[:-1] + "q"
        out.append(("when is " + near + " ")[:500] * 3)
        out.append((near + " lecture today ") * 40)
    return out


def test_the_router_is_total_on_generated_strings() -> None:
    strings = generated_strings()
    assert len(strings) >= 3000
    contexts = [make_ctx(r) for r in ("student", "teacher", "admin")] + [hostile_context(r) for r in ("student", "teacher", "admin", "root")]
    for i, text in enumerate(strings):
        ctx = contexts[i % len(contexts)]
        d = route(text, ctx)  # must not raise
        check_invariants(d, ctx)


def test_the_router_gives_the_same_answer_for_the_same_input() -> None:
    ctx = make_ctx("admin")
    for text in generated_strings()[:400]:
        assert route(text, ctx) == route(text, ctx)


def test_a_string_with_only_ordinary_content_after_a_huge_prefix_is_still_bounded() -> None:
    ctx = make_ctx("admin")
    d = route("x" * 100_000 + " next lecture", ctx)
    assert d.route == "rag"


def test_non_string_input_does_not_raise() -> None:
    ctx = make_ctx()
    for value in (None, 12, b"next lecture", ["next lecture"], object()):
        d = route(value, ctx)  # type: ignore[arg-type]
        assert d.route == "rag"


def test_an_internal_error_is_a_recorded_rag_decision_and_never_escapes(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> RouteDecision:
        raise RuntimeError("bug")

    monkeypatch.setattr(chat_router, "_route", boom)
    d = route("next lecture", make_ctx())
    assert d.route == "rag" and d.confidence == "low"
    assert d.rule_ids == ("miss.router_error",)


def test_a_broken_context_does_not_escape_either() -> None:
    bad = RouterContext(role="student", classes=None, subjects=(), timezone=IST, now=NOW)  # type: ignore[arg-type]
    d = route("when is physics", bad)
    assert d.route == "rag" and d.rule_ids == ("miss.router_error",)
    text_zone = RouterContext(role="student", classes=(), subjects=(), timezone="Asia/Kolkata", now=NOW)  # type: ignore[arg-type]
    d = route("today's timetable", text_zone)
    assert d.route == "rag" and d.rule_ids == ("miss.router_error",)


# ── time: the router never reads a clock ──────────────────────────────────────


def test_today_tomorrow_and_weekday_follow_the_context_clock() -> None:
    later = datetime(2027, 3, 1, 9, 0, tzinfo=IST)  # a Monday
    ctx = make_ctx(now=later)
    assert route("today's timetable", ctx).day == date(2027, 3, 1)
    assert route("tomorrow's timetable", ctx).day == date(2027, 3, 2)
    assert route("monday timetable", ctx).day == date(2027, 3, 1)  # today is Monday: today
    assert route("sunday timetable", ctx).day == date(2027, 3, 7)
    assert route("tuesday timetable", ctx).day == date(2027, 3, 2)


def test_the_day_is_the_day_in_the_tenant_timezone() -> None:
    utc_evening = datetime(2026, 11, 25, 23, 30, tzinfo=UTC)  # 05:00 on the 26th in Kolkata
    assert route("today's timetable", make_ctx(now=utc_evening, tz=IST)).day == date(2026, 11, 26)
    assert route("today's timetable", make_ctx(now=utc_evening, tz=UTC)).day == date(2026, 11, 25)
    assert route("tomorrow's timetable", make_ctx(now=utc_evening, tz=IST)).day == date(2026, 11, 27)
    assert route("friday timetable", make_ctx(now=utc_evening, tz=IST)).day == date(2026, 11, 27)


def test_a_naive_now_is_read_as_tenant_local_time() -> None:
    naive = datetime(2026, 11, 25, 23, 30)
    assert route("today's timetable", make_ctx(now=naive)).day == date(2026, 11, 25)


def test_day_is_only_set_for_the_intents_that_have_one() -> None:
    ctx = make_ctx()
    assert route("next lecture", ctx).day is None
    assert route("what is on now", ctx).day is None
    assert route("when is physics", ctx).day is None
    assert route("today's timetable", ctx).day == TODAY


@pytest.mark.parametrize(
    "text",
    [
        "today tomorrow timetable",
        "monday and tuesday timetable",
        "next lecture now",
        "now today timetable",
        "tomorrow wednesday schedule",
    ],
)
def test_two_different_days_in_one_question_is_a_recorded_miss(text: str) -> None:
    ctx = make_ctx()
    d = route(text, ctx)
    check_invariants(d, ctx)
    assert d.route == "rag" and "miss.conflicting_time" in d.rule_ids, d


def test_the_same_day_word_twice_is_not_a_conflict() -> None:
    assert route("today's lectures today", make_ctx()).intent == "today"
    assert route("next lecture upcoming class", make_ctx()).intent == "next_lecture"


def test_a_day_word_beats_next_when_both_are_there() -> None:
    d = route("next lecture tomorrow", make_ctx())
    assert (d.intent, d.day) == ("tomorrow", TOMORROW)
    d = route("next monday lecture", make_ctx())
    assert (d.intent, d.day) == ("weekday", DAYS["mon"])


# ── rules ─────────────────────────────────────────────────────────────────────


def test_rule_ids_are_unique() -> None:
    assert len(RULE_IDS) == len(set(RULE_IDS)), sorted({r for r in RULE_IDS if RULE_IDS.count(r) > 1})
    assert all(r and r == r.strip() and " " not in r for r in RULE_IDS)


def test_every_phrase_belongs_to_one_rule_or_filler() -> None:
    assert DEFAULT_INDEX.conflicts == ()


def test_every_language_table_has_rules_with_its_own_prefix() -> None:
    for table in chat_router_phrases.LANGUAGE_TABLES:
        assert table.rules and table.fillers
        for rule in table.rules:
            assert rule.rule_id.startswith(table.code + "."), rule.rule_id
            assert rule.phrases, rule.rule_id


def test_every_rule_id_is_produced_by_some_case() -> None:
    seen: set[str] = set()
    all_cases = EN_CASES + MR_CASES
    for case in all_cases:
        seen.update(route(case.text, make_ctx(case.role)).rule_ids)
    for text in EN_PLAIN_RAG + EN_MISS_RAG + MR_PLAIN_RAG + MR_MISS_RAG:
        seen.update(route(text, make_ctx()).rule_ids)
    for text in [
        "timetable for 10-A",
        "सर्व वर्गांचे वेळापत्रक",
        "timetable for all classes",
        "sarva varg velapatrak",
        "next lecture for 10-A and 10-B",
        "when is physics or chemistry",
        "today tomorrow timetable",
    ]:
        for role in ("student", "admin"):
            seen.update(route(text, make_ctx(role)).rule_ids)
    seen.update(route("which room", make_ctx()).rule_ids)
    seen.update(route("when is my lecture", make_ctx()).rule_ids)
    never = set(RULE_IDS) - seen
    assert never == {"miss.router_error"}, sorted(never)  # that one needs a fault and has its own test


def test_a_schedule_decision_lists_the_rules_that_fired_and_ends_with_the_intent_rule() -> None:
    d = route("what's my next lecture", make_ctx())
    assert d.rule_ids == ("en.order.next", "en.noun.lecture", "intent.next_lecture")
    d = route("when is physics", make_ctx())
    assert d.rule_ids == ("en.when.when", "name.subject", "intent.subject_when")
    d = route("आजचे वेळापत्रक", make_ctx())
    assert d.rule_ids == ("mr.time.today", "mr.noun.timetable", "intent.today")


def test_the_schedule_noun_is_required() -> None:
    ctx = make_ctx()
    for text in ["next", "today", "tomorrow", "now", "monday", "next today", "what is on today", "when is next", "tomorrow monday"]:
        assert route(text, ctx).route == "rag", text
    for text in ["next lecture", "today timetable", "tomorrow class", "lecture now", "monday schedule"]:
        assert route(text, ctx).route == "schedule", text


def test_a_time_or_order_word_is_required() -> None:
    ctx = make_ctx()
    for text in ["lecture", "timetable", "class", "my timetable", "show the schedule", "the classroom"]:
        assert route(text, ctx).route == "rag", text


def test_the_router_has_no_open_ended_tail() -> None:
    # every word the question holds must be known (schedule words, names of the caller, filler words)
    ctx = make_ctx()
    assert route("next lecture", ctx).route == "schedule"
    assert route("next lecture zebra", ctx).route == "rag"
    assert route("zebra next lecture", ctx).route == "rag"


# ── purity: imports and calls are read from the source ────────────────────────

SERVICES = Path(chat_router.__file__).parent
MODULES = {
    "chat_router": Path(chat_router.__file__),
    "chat_router_phrases": Path(chat_router_phrases.__file__),
}
FORBIDDEN_PREFIXES = (
    "sqlalchemy",
    "httpx",
    "redis",
    "app.db",
    "app.llm",
    "app.services.schedule",
    "time",
    "random",
    "secrets",
    "os",
    "pathlib",
    "io",
    "socket",
    "subprocess",
    "asyncio",
    "logging",
    "zoneinfo",
    "requests",
    "fastapi",
)
ALLOWED_IMPORTS = {
    "chat_router": {
        "__future__",
        "dataclasses",
        "datetime",
        "difflib",
        "typing",
        "unicodedata",
        "uuid",
        "collections.abc",
        "app.services.chat_router_phrases",
        "app.services.document_text",
    },
    "chat_router_phrases": {"__future__", "collections.abc", "dataclasses", "typing"},
}
CLOCK_AND_IO_CALLS = {
    "now",
    "today",
    "utcnow",
    "time",
    "time_ns",
    "monotonic",
    "perf_counter",
    "process_time",
    "random",
    "randint",
    "randrange",
    "choice",
    "shuffle",
    "uuid1",
    "uuid4",
    "urandom",
    "open",
    "read_text",
    "read_bytes",
    "getenv",
    "ZoneInfo",
    "fromtimestamp",
    "utcfromtimestamp",
}


def imported_modules(path: Path, expand: bool = False) -> set[str]:
    """Module names a file imports. With expand, `from app.services import schedule` also lists
    app.services.schedule (the module that name would load)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not used in app/"
            base = node.module or ""
            found.add(base)
            if expand:
                found.update(f"{base}.{a.name}" for a in node.names)
    return found


def is_forbidden(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PREFIXES)


@pytest.mark.parametrize("module", sorted(MODULES))
def test_router_modules_import_only_pure_things(module: str) -> None:
    bad = {n for n in imported_modules(MODULES[module], expand=True) if is_forbidden(n)}
    assert not bad, f"{module} imports {sorted(bad)}"
    # the allow-list is stricter than the deny-list: a new import has to be added here on purpose
    unexpected = imported_modules(MODULES[module]) - ALLOWED_IMPORTS[module]
    assert not unexpected, f"{module} imports something not on the pure list: {sorted(unexpected)}"


def _module_file(name: str) -> Path | None:
    candidate = SERVICES.parent.parent / Path(*name.split("."))
    if candidate.with_suffix(".py").exists():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").exists():
        return candidate / "__init__.py"
    return None


TRANSITIVE_FORBIDDEN = ("sqlalchemy", "httpx", "redis", "requests", "app.db", "app.llm", "app.services.schedule")


def test_nothing_the_router_loads_reaches_the_database_the_network_or_the_llm() -> None:
    """Transitive: every app module the router imports (and what those import) is scanned too."""
    todo = [MODULES["chat_router"]]
    seen: set[Path] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in imported_modules(path, expand=True):
            bad = any(name == p or name.startswith(p + ".") for p in TRANSITIVE_FORBIDDEN)
            assert not bad, f"{path.name} imports {name}"
            if name.startswith("app"):
                parent = name
                while parent:  # the module itself and every package above it run when it is imported
                    file = _module_file(parent)
                    if file is not None:
                        todo.append(file)
                    parent = parent.rpartition(".")[0] if "." in parent else ""
    assert len(seen) >= 3


@pytest.mark.parametrize("module", sorted(MODULES))
def test_router_modules_read_no_clock_no_random_no_file(module: str) -> None:
    tree = ast.parse(MODULES[module].read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            assert name not in CLOCK_AND_IO_CALLS, f"{module} calls {name}() at line {node.lineno}"
        if isinstance(node, ast.Global | ast.Nonlocal):
            raise AssertionError(f"{module} uses global state at line {node.lineno}")


def test_the_router_does_not_call_the_schedule_service() -> None:
    source = MODULES["chat_router"].read_text(encoding="utf-8")
    for name in ("whats_on_now", "todays_schedule", "schedule_for_day", "class_schedule", "next_lecture(", "resolve_authorized_class_ids"):
        assert name not in source.replace("intent.next_lecture", "").replace('"next_lecture"', ""), name


def test_route_has_the_contract_signature() -> None:
    import inspect

    params = list(inspect.signature(route).parameters)
    assert params[:2] == ["text", "ctx"]
    assert [f.name for f in dataclasses.fields(RouteDecision)][:7] == [
        "route",
        "intent",
        "class_ids",
        "subject_id",
        "day",
        "rule_ids",
        "confidence",
    ]
    assert RouteDecision.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert RouterContext.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert NamedRef.__dataclass_params__.frozen  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        route("next lecture", make_ctx()).route = "rag"  # type: ignore[misc]
