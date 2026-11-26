"""
app/services/chat_router.py
───────────────────────────
The chat router (plan 3). Step 6a-7. It decides, with rules and no model, whether a question is a timetable
question that the schedule service can answer ("schedule"), a timetable question that names more than one class or
subject ("clarify"), or anything else ("rag").

    route(text, ctx) -> RouteDecision

PURE. No database, network, clock, random or file access; the caller passes `now` and the tenant's timezone object
in the context (a ZoneInfo is built by the caller: constructing one reads a file). The module imports nothing from
app.db, app.llm or app.services.schedule, and it does not decide who may see what: the classes and subjects in the
context are what the caller is ALREADY authorised for, and they are the only names this module can ever match.
A name outside that scope cannot be matched, so it behaves exactly like a name that does not exist (no probing).

TOTAL. route() returns a RouteDecision for any input and never raises. A bug inside the router is caught and
recorded as a rag decision with the rule id "miss.router_error" (a decision that carries the id gets stored on the
message row, where an operator can count it). route_with_error() also gives the error's class name, which the
message route (6a-9b) logs as chat_router_error; this module itself never logs.

How a question is decided (plan 3.3):
  1. Truncate to MAX_INPUT_CHARS, then normalise: ingest clean_text rules (invisible and control characters out,
     one ASCII space), NFKC, case fold, apostrophes dropped ("today's" -> "todays"), punctuation and symbols to
     spaces, any decimal digit to ASCII, a space between letters and digits ("10A" -> "10 a"), spaces collapsed.
  2. Read the words with the phrase tables (chat_router_phrases): longest phrase first, then names of the caller's
     classes and subjects (difflib ratio >= NAME_MATCH_RATIO, exact match preferred), then filler words. Any other
     word is "unexplained".
  3. A schedule decision needs a candidate: a schedule noun plus a time or order word; a subject plus "when" (or plus
     a noun and a time word); a subject plus a room noun; a class (or "all classes") plus a timetable noun. Without
     a candidate the answer is plain rag and lists no rule.
  4. With a candidate, ANY unexplained word makes it a miss: rag, low confidence, the rules that fired plus
     "miss.unexplained_tokens". That is what keeps "what is the next class in the inheritance hierarchy" and
     "when was the exam of physics" away from the timetable.
  5. Two or more matching classes or subjects: clarify (the ids are the authorised candidates).

The decision lists, in the order the words were read, the rules that fired and last the intent rule
("intent.<intent>"). Confidence is "high" for schedule and clarify and "low" for every rag decision.
"""

from __future__ import annotations

import difflib
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Final, Literal

from app.services.chat_router_phrases import (
    ALL_CLASSES,
    LANGUAGE_TABLES,
    NEXT,
    NOUN_LECTURE,
    NOUN_ROOM,
    NOUN_TIMETABLE,
    NOW,
    NOW_PHRASE,
    TODAY,
    TOMORROW,
    WEEKDAY,
    WHEN,
    LanguageTable,
    PhraseRule,
)
from app.services.document_text import clean_text

MAX_INPUT_CHARS: Final = 500
NAME_MATCH_RATIO: Final = 0.85
# Logged with chat_router_error and chat_turn (6a-9b), so an operator can tell which rules made a decision.
ROUTER_VERSION: Final = "router-v1"

RoleKind = Literal["student", "teacher", "admin"]
Route = Literal["schedule", "clarify", "rag"]
Confidence = Literal["high", "low"]

INTENTS: Final = (
    "next_lecture",
    "now",
    "today",
    "tomorrow",
    "weekday",
    "subject_when",
    "room",
    "class_named",
)
_DAY_INTENTS: Final = frozenset({"today", "tomorrow", "weekday", "subject_when", "room"})
_CLASS_NAMED_ROLES: Final = frozenset({"teacher", "admin"})

INTENT_RULE_IDS: Final = tuple(f"intent.{name}" for name in INTENTS)
NAME_RULE_IDS: Final = ("name.class", "name.subject")
MISS_UNEXPLAINED: Final = "miss.unexplained_tokens"
MISS_CONFLICT: Final = "miss.conflicting_time"
MISS_ROLE: Final = "miss.role_not_allowed"
MISS_NO_TARGET: Final = "miss.no_target"
MISS_ERROR: Final = "miss.router_error"
MISS_RULE_IDS: Final = (MISS_UNEXPLAINED, MISS_CONFLICT, MISS_ROLE, MISS_NO_TARGET, MISS_ERROR)


# ── types ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NamedRef:
    """A class or a subject the caller may see: its id and its name."""

    id: uuid.UUID
    name: str


@dataclass(frozen=True, slots=True)
class RouterContext:
    """Everything the router may use besides the text. `classes` and `subjects` are the AUTHORISED ones only."""

    role: RoleKind
    classes: tuple[NamedRef, ...]
    subjects: tuple[NamedRef, ...]
    timezone: tzinfo
    now: datetime


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The plan's contract (3.1). `class_ids` None means "all of the caller's classes" for a schedule decision.
    `subject_ids` is filled on clarify only, when more than one subject matched (then `subject_id` is None)."""

    route: Route
    intent: str | None
    class_ids: tuple[uuid.UUID, ...] | None
    subject_id: uuid.UUID | None
    day: date | None
    rule_ids: tuple[str, ...]
    confidence: Confidence
    subject_ids: tuple[uuid.UUID, ...] = ()


_PLAIN_RAG: Final = RouteDecision("rag", None, None, None, None, (), "low")
_ERROR_RAG: Final = RouteDecision("rag", None, None, None, None, (MISS_ERROR,), "low")


# ── normalisation ─────────────────────────────────────────────────────────────

_APOSTROPHES: Final = frozenset("'’‘ʼ`´")


def normalise(text: str) -> str:
    """The form in which questions, names and phrases are compared (plan 3.3 step 1). Words are joined by single
    spaces; the result is "" when nothing is left."""
    text = clean_text(text)
    text = unicodedata.normalize("NFKC", text).casefold()
    text = clean_text(unicodedata.normalize("NFKC", text))
    out: list[str] = []
    previous = "O"  # D digit, L letter, M mark, O anything else
    for char in text:
        if char in _APOSTROPHES:
            continue
        category = unicodedata.category(char)
        if category == "Nd":
            digit = unicodedata.decimal(char, None)
            if digit is not None:
                char = str(digit)
        if char.isascii() and char.isdigit():
            kind = "D"
        elif category[0] == "L":
            kind = "L"
        elif category[0] == "M":
            kind = "M"
        elif category[0] in "PSZ":
            out.append(" ")
            previous = "O"
            continue
        else:
            kind = "O"
        if (previous == "D" and kind == "L") or (previous in "LM" and kind == "D"):
            out.append(" ")
        out.append(char)
        previous = kind
    return " ".join("".join(out).split())


# ── the phrase index ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PhraseIndex:
    """The tables, looked up by words. `rule_ids` keeps duplicates on purpose so that a test can see them;
    `conflicts` lists phrases that two rules claim."""

    rules: dict[tuple[str, ...], PhraseRule]
    fillers: frozenset[str]
    max_len: int
    rule_ids: tuple[str, ...]
    conflicts: tuple[tuple[str, ...], ...]


def build_phrase_index(tables: Sequence[LanguageTable]) -> PhraseIndex:
    rules: dict[tuple[str, ...], PhraseRule] = {}
    fillers: set[str] = set()
    rule_ids: list[str] = []
    conflicts: list[tuple[str, ...]] = []
    for table in tables:
        for rule in table.rules:
            rule_ids.append(rule.rule_id)
            for phrase in rule.phrases:
                words = tuple(normalise(phrase).split())
                if not words:
                    continue
                known = rules.get(words)
                if known is None:
                    rules[words] = rule
                elif known.rule_id != rule.rule_id:
                    conflicts.append(words)
        for filler in table.fillers:
            fillers.update(normalise(filler).split())
    rule_ids.extend(INTENT_RULE_IDS)
    rule_ids.extend(NAME_RULE_IDS)
    rule_ids.extend(MISS_RULE_IDS)
    return PhraseIndex(
        rules=rules,
        fillers=frozenset(fillers),
        max_len=max((len(words) for words in rules), default=1),
        rule_ids=tuple(rule_ids),
        conflicts=tuple(conflicts),
    )


DEFAULT_INDEX: Final = build_phrase_index(LANGUAGE_TABLES)
RULE_IDS: Final = DEFAULT_INDEX.rule_ids


# ── names ─────────────────────────────────────────────────────────────────────


class _NameKey:
    """One distinct normalised name and everything (class or subject) that carries it."""

    __slots__ = ("entities", "k", "matcher", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self.k = len(text.split())
        self.entities: list[tuple[str, uuid.UUID]] = []
        self.matcher = difflib.SequenceMatcher(None, "", text, autojunk=False)

    def score(self, window: str) -> float:
        """difflib ratio of `window` against the name, 0.0 when it cannot reach the limit."""
        if window == self.text:
            return 1.0
        self.matcher.set_seq1(window)
        if self.matcher.real_quick_ratio() < NAME_MATCH_RATIO or self.matcher.quick_ratio() < NAME_MATCH_RATIO:
            return 0.0
        return self.matcher.ratio()


@dataclass(frozen=True, slots=True)
class _NameSpan:
    end: int
    entities: tuple[tuple[str, uuid.UUID], ...]


def _name_keys(ctx: RouterContext, index: PhraseIndex) -> list[_NameKey]:
    keys: dict[str, _NameKey] = {}
    for kind, refs in (("class", ctx.classes), ("subject", ctx.subjects)):
        for named in refs:
            text = normalise(named.name)
            words = text.split()
            # a name made only of ordinary words ("A", "Today", "next lecture") would swallow the question's own words
            if not words or all(w in index.fillers or (w,) in index.rules for w in words):
                continue
            key = keys.get(text)
            if key is None:
                key = keys[text] = _NameKey(text)
            key.entities.append((kind, named.id))
    return list(keys.values())


def _find_names(tokens: tuple[str, ...], ctx: RouterContext, index: PhraseIndex) -> dict[int, _NameSpan]:
    """Spans of the question that name one of the caller's classes or subjects, left to right."""
    keys = _name_keys(ctx, index)
    spans: dict[int, _NameSpan] = {}
    if not keys:
        return spans
    count = len(tokens)
    windows: dict[tuple[int, int], str] = {}
    i = 0
    while i < count:
        scored: list[tuple[float, _NameKey]] = []
        for key in keys:
            if i + key.k > count:
                continue
            window = windows.get((i, key.k))
            if window is None:
                window = windows[(i, key.k)] = " ".join(tokens[i : i + key.k])
            score = key.score(window)
            if score >= NAME_MATCH_RATIO:
                scored.append((score, key))
        if not scored:
            i += 1
            continue
        exact = [pair for pair in scored if pair[0] == 1.0]
        if exact:
            chosen_k = max(key.k for _, key in exact)
            chosen = [key for _, key in exact if key.k == chosen_k]
        else:
            top = max(scored, key=lambda pair: (pair[0], pair[1].k))
            chosen_k = top[1].k
            chosen = [key for _, key in scored if key.k == chosen_k]
        entities: list[tuple[str, uuid.UUID]] = []
        for key in chosen:
            entities.extend(key.entities)
        spans[i] = _NameSpan(end=i + chosen_k, entities=tuple(entities))
        i += chosen_k
    return spans


# ── reading the words ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Scan:
    hits: list[PhraseRule]
    fired: list[str]
    unknown: int
    class_ids: list[uuid.UUID]
    subject_ids: list[uuid.UUID]


def _consume(tokens: tuple[str, ...], index: PhraseIndex, names: dict[int, _NameSpan]) -> _Scan:
    scan = _Scan(hits=[], fired=[], unknown=0, class_ids=[], subject_ids=[])
    count = len(tokens)
    i = 0
    while i < count:
        span = names.get(i)
        if span is not None:
            for kind, entity_id in span.entities:
                target = scan.class_ids if kind == "class" else scan.subject_ids
                if entity_id not in target:
                    target.append(entity_id)
                scan.fired.append("name.class" if kind == "class" else "name.subject")
            i = span.end
            continue
        matched = False
        for length in range(min(index.max_len, count - i), 0, -1):
            rule = index.rules.get(tokens[i : i + length])
            if rule is not None:
                scan.hits.append(rule)
                scan.fired.append(rule.rule_id)
                i += length
                matched = True
                break
        if matched:
            continue
        if tokens[i] not in index.fillers:
            scan.unknown += 1
        i += 1
    return scan


# ── deciding ──────────────────────────────────────────────────────────────────

_TIME_KINDS: Final = frozenset({NEXT, NOW, NOW_PHRASE, TODAY, TOMORROW, WEEKDAY})


def _miss(fired: Sequence[str], miss_id: str) -> RouteDecision:
    return RouteDecision("rag", None, None, None, None, (*dict.fromkeys(fired), miss_id), "low")


def _local_today(ctx: RouterContext) -> date:
    now = ctx.now
    local = now.astimezone(ctx.timezone) if now.tzinfo is not None else now.replace(tzinfo=ctx.timezone)
    return local.date()


def _day_for(key: tuple[str, int], ctx: RouterContext) -> date:
    today = _local_today(ctx)
    if key[0] == "today":
        return today
    if key[0] == "tomorrow":
        return today + timedelta(days=1)
    return today + timedelta(days=(key[1] - today.weekday()) % 7)


def _decide(scan: _Scan, ctx: RouterContext) -> RouteDecision:
    kinds = {rule.kind for rule in scan.hits}
    noun_room = NOUN_ROOM in kinds
    noun_slot = NOUN_LECTURE in kinds or NOUN_TIMETABLE in kinds or NOW_PHRASE in kinds
    has_noun = noun_slot or noun_room
    has_time = bool(kinds & _TIME_KINDS)
    has_when = WHEN in kinds
    subjects, classes = scan.subject_ids, scan.class_ids
    all_classes = ALL_CLASSES in kinds
    named_class = bool(classes) or all_classes
    slot_noun_for_class = NOUN_LECTURE in kinds or NOUN_TIMETABLE in kinds

    room_candidate = bool(subjects) and noun_room
    subject_candidate = bool(subjects) and (has_when or (noun_slot and has_time))
    time_candidate = has_noun and has_time
    when_candidate = has_noun and has_when
    class_candidate = named_class and slot_noun_for_class and not subjects
    if not (room_candidate or subject_candidate or time_candidate or when_candidate or class_candidate):
        return _PLAIN_RAG

    fired = tuple(dict.fromkeys(scan.fired))
    if scan.unknown:
        return _miss(fired, MISS_UNEXPLAINED)

    # time words: one day at most, and "now" does not mix with a day or with "next"
    day_keys: list[tuple[str, int]] = []
    for rule in scan.hits:
        if rule.kind == TODAY:
            key = ("today", 0)
        elif rule.kind == TOMORROW:
            key = ("tomorrow", 0)
        elif rule.kind == WEEKDAY and rule.value is not None:
            key = ("weekday", rule.value)
        else:
            continue
        if key not in day_keys:
            day_keys.append(key)
    has_now = NOW in kinds or NOW_PHRASE in kinds
    if len(day_keys) > 1 or (has_now and (day_keys or NEXT in kinds)):
        return _miss(fired, MISS_CONFLICT)

    if room_candidate:
        intent = "room"
    elif subject_candidate:
        intent = "subject_when"
    elif time_candidate:
        if day_keys:
            intent = day_keys[0][0]
        elif has_now:
            intent = "now"
        else:
            intent = "next_lecture"
    elif when_candidate:
        return _miss(fired, MISS_NO_TARGET)
    else:
        if ctx.role not in _CLASS_NAMED_ROLES:
            return _miss(fired, MISS_ROLE)
        intent = "class_named"

    day = _day_for(day_keys[0], ctx) if intent in _DAY_INTENTS and len(day_keys) == 1 else None
    ambiguous_classes = len(classes) > 1
    ambiguous_subjects = len(subjects) > 1
    return RouteDecision(
        route="clarify" if (ambiguous_classes or ambiguous_subjects) else "schedule",
        intent=intent,
        class_ids=tuple(classes) if classes else None,
        subject_id=subjects[0] if len(subjects) == 1 else None,
        day=day,
        rule_ids=(*fired, f"intent.{intent}"),
        confidence="high",
        subject_ids=tuple(subjects) if ambiguous_subjects else (),
    )


def _route(text: str, ctx: RouterContext, index: PhraseIndex) -> RouteDecision:
    if not isinstance(text, str):
        return _PLAIN_RAG
    tokens = tuple(normalise(text[:MAX_INPUT_CHARS]).split())
    if not tokens:
        return _PLAIN_RAG
    scan = _consume(tokens, index, {})
    if not scan.hits:  # no schedule word at all: nothing to do, and no name is looked at
        return _PLAIN_RAG
    names = _find_names(tokens, ctx, index)
    if names:
        scan = _consume(tokens, index, names)
    return _decide(scan, ctx)


def route_with_error(
    text: str, ctx: RouterContext, *, index: PhraseIndex = DEFAULT_INDEX
) -> tuple[RouteDecision, str | None]:
    """route() plus the CLASS NAME of an internal error (None when there was none), so the caller can log
    chat_router_error (plan 3.5, 10.1). Only the class name leaves this function: never the exception's message,
    which could hold the question. Never raises."""
    try:
        return _route(text, ctx, index), None
    except Exception as exc:
        return _ERROR_RAG, type(exc).__name__


def route(text: str, ctx: RouterContext, *, index: PhraseIndex = DEFAULT_INDEX) -> RouteDecision:
    """Route one question. Never raises (see the module docstring)."""
    return route_with_error(text, ctx, index=index)[0]
