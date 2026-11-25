"""
app/services/chat_router_phrases.py
───────────────────────────────────
The phrase tables of the chat router (plan 3.3). Step 6a-7. Matching data only: nothing here is shown to a user
(the answer templates are step 6a-9b).

A language is ONE LanguageTable. To add a language, write another table and append it to LANGUAGE_TABLES; the router
code does not change. English and Marathi are shipped (Hindi waits for owner question Q5, so there is no Hindi table).
Romanised Marathi is Latin text: its words sit in the Marathi table next to the Devanagari ones, and the router looks
a word up in every table, so a mixed question ("maza next lecture kadhi aahe") works.

Each rule has an id ("<language>.<kind>.<key>") that the router stores on the message as `rule_ids`. A rule is a kind
plus the phrases that trigger it (space separated words; the router normalises them like it normalises a question, so
write them in any case and with apostrophes if you like). `fillers` are the words that carry no meaning of their own
("my", "please", "कृपया"): the router allows them around a schedule phrase but never starts a decision on them.

This module imports nothing but dataclasses, collections.abc and typing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

# ── kinds ─────────────────────────────────────────────────────────────────────

NOUN_LECTURE: Final = "noun_lecture"  # lecture, class, period: one teaching slot
NOUN_TIMETABLE: Final = "noun_timetable"  # timetable, schedule
NOUN_ROOM: Final = "noun_room"  # room
NEXT: Final = "next"  # next, upcoming
NOW: Final = "now"  # now, current
NOW_PHRASE: Final = "now_phrase"  # a whole question that is a schedule noun and "now" at once ("what's on now")
TODAY: Final = "today"
TOMORROW: Final = "tomorrow"
WEEKDAY: Final = "weekday"  # `value` is the weekday number, Monday = 0
WHEN: Final = "when"  # when, what time
ALL_CLASSES: Final = "all_classes"  # "all classes"

KINDS: Final = (
    NOUN_LECTURE,
    NOUN_TIMETABLE,
    NOUN_ROOM,
    NEXT,
    NOW,
    NOW_PHRASE,
    TODAY,
    TOMORROW,
    WEEKDAY,
    WHEN,
    ALL_CLASSES,
)


@dataclass(frozen=True, slots=True)
class PhraseRule:
    rule_id: str
    kind: str
    phrases: tuple[str, ...]
    value: int | None = None


@dataclass(frozen=True, slots=True)
class LanguageTable:
    code: str
    rules: tuple[PhraseRule, ...]
    fillers: tuple[str, ...]


def _weekday_rules(
    code: str, names: Mapping[int, tuple[str, ...]], suffixes: tuple[str, ...], extra: Mapping[int, tuple[str, ...]]
) -> tuple[PhraseRule, ...]:
    keys = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    rules = []
    for number, key in enumerate(keys):
        phrases = [stem + suffix for stem in names[number] for suffix in suffixes]
        phrases.extend(extra.get(number, ()))
        rules.append(PhraseRule(f"{code}.weekday.{key}", WEEKDAY, tuple(phrases), number))
    return tuple(rules)


# ── English ───────────────────────────────────────────────────────────────────

_EN_DAYS = {
    0: ("monday",),
    1: ("tuesday",),
    2: ("wednesday",),
    3: ("thursday",),
    4: ("friday",),
    5: ("saturday",),
    6: ("sunday",),
}
_EN_DAY_SHORT = {
    0: ("mon",),
    1: ("tue", "tues"),
    2: ("wed",),
    3: ("thu", "thur", "thurs"),
    4: ("fri",),
    5: ("sat",),
    6: ("sun",),
}

ENGLISH = LanguageTable(
    code="en",
    rules=(
        PhraseRule(
            "en.noun.lecture",
            NOUN_LECTURE,
            ("lecture", "lectures", "class", "classes", "period", "periods", "session", "sessions"),
        ),
        PhraseRule(
            "en.noun.timetable",
            NOUN_TIMETABLE,
            ("timetable", "timetables", "time table", "time tables", "schedule", "schedules"),
        ),
        PhraseRule("en.noun.room", NOUN_ROOM, ("room", "rooms", "classroom", "classrooms")),
        PhraseRule("en.order.next", NEXT, ("next", "upcoming")),
        PhraseRule(
            "en.time.now",
            NOW,
            ("now", "right now", "currently", "current", "ongoing", "at the moment"),
        ),
        PhraseRule(
            "en.time.now_phrase",
            NOW_PHRASE,
            ("what's on now", "what's on right now", "what is on now", "what is on right now"),
        ),
        PhraseRule("en.time.today", TODAY, ("today", "today's")),
        PhraseRule("en.time.tomorrow", TOMORROW, ("tomorrow", "tomorrow's", "tommorow", "tomorow", "tmrw")),
        *_weekday_rules("en", {n: (d[0],) for n, d in _EN_DAYS.items()}, ("", "'s"), _EN_DAY_SHORT),
        PhraseRule("en.when.when", WHEN, ("when", "when's", "what time", "at what time")),
        PhraseRule(
            "en.all.classes",
            ALL_CLASSES,
            (
                "all classes",
                "all class",
                "every class",
                "all the classes",
                "all my classes",
                "all of my classes",
                "all of the classes",
                "each class",
            ),
        ),
    ),
    fillers=(
        "a", "an", "the", "my", "our", "me", "i", "we", "us", "you", "your", "is", "are", "am", "be",
        "do", "does", "have", "has", "there", "any", "what", "what's", "which", "where", "tell", "show",
        "give", "list", "please", "pls", "plz", "kindly", "can", "could", "would", "will", "for",
        "of", "in", "on", "at", "to", "it", "its", "sir", "madam", "mam", "maam", "hi", "hello", "hey",
        "thanks", "thank", "ok", "okay", "and", "or", "also",
    ),
)

# ── Marathi (Devanagari and Romanised) ────────────────────────────────────────

_MR_DAYS_DEVANAGARI = {
    0: ("सोमवार",),
    1: ("मंगळवार",),
    2: ("बुधवार",),
    3: ("गुरुवार", "गुरूवार"),
    4: ("शुक्रवार",),
    5: ("शनिवार",),
    6: ("रविवार",),
}
_MR_DAYS_LATIN = {
    0: ("somvar", "somwar"),
    1: ("mangalvar", "mangalwar"),
    2: ("budhvar", "budhwar"),
    3: ("guruvar", "guruwar"),
    4: ("shukravar", "shukrawar"),
    5: ("shanivar", "shaniwar"),
    6: ("ravivar", "raviwar"),
}
_MR_DEVANAGARI_SUFFIXES = ("", "चा", "चे", "ची", "च्या", "चं", "ी", "ला")
_MR_LATIN_SUFFIXES = ("", "cha", "che", "chi", "chya", "la", "i")


def _marathi_weekdays() -> tuple[PhraseRule, ...]:
    devanagari = _weekday_rules("mr", _MR_DAYS_DEVANAGARI, _MR_DEVANAGARI_SUFFIXES, {})
    latin = _weekday_rules("mr", _MR_DAYS_LATIN, _MR_LATIN_SUFFIXES, {})
    return tuple(
        PhraseRule(dev.rule_id, WEEKDAY, dev.phrases + lat.phrases, dev.value)
        for dev, lat in zip(devanagari, latin, strict=True)
    )


MARATHI = LanguageTable(
    code="mr",
    rules=(
        PhraseRule(
            "mr.noun.lecture",
            NOUN_LECTURE,
            (
                "तास",
                "तासिका",
                "लेक्चर",
                "लेक्चर्स",
                "क्लास",
                "पिरियड",
                "पीरियड",
                "वर्ग",
                "taas",
                "tas",
                "tasika",
                "taasika",
                "varg",
            ),
        ),
        PhraseRule(
            "mr.noun.timetable",
            NOUN_TIMETABLE,
            ("वेळापत्रक", "टाइमटेबल", "टाईमटेबल", "टाइम टेबल", "टाईम टेबल", "velapatrak", "vela patrak"),
        ),
        PhraseRule(
            "mr.noun.room",
            NOUN_ROOM,
            (
                "खोली",
                "खोलीत",
                "खोलीमध्ये",
                "वर्गखोली",
                "वर्गखोलीत",
                "रूम",
                "रुम",
                "रूममध्ये",
                "रुममध्ये",
                "कक्ष",
                "कक्षात",
                "kholi",
                "kholit",
                "kholimadhe",
            ),
        ),
        PhraseRule(
            "mr.order.next",
            NEXT,
            (
                "पुढचा",
                "पुढचे",
                "पुढची",
                "पुढच्या",
                "पुढचं",
                "पुढील",
                "pudhcha",
                "pudhche",
                "pudhchi",
                "pudhchya",
                "pudhil",
                "pudhacha",
                "pudhache",
            ),
        ),
        PhraseRule(
            "mr.time.now",
            NOW,
            (
                "आता",
                "आत्ता",
                "सध्या",
                "चालू",
                "आताचा",
                "आताचे",
                "आताची",
                "aata",
                "aatta",
                "ata",
                "sadhya",
                "chalu",
            ),
        ),
        PhraseRule(
            "mr.time.now_phrase",
            NOW_PHRASE,
            (
                "आता काय आहे",
                "आता काय चालू आहे",
                "aata kay aahe",
                "aata kay ahe",
                "aata kay chalu aahe",
                "ata kay aahe",
            ),
        ),
        PhraseRule(
            "mr.time.today",
            TODAY,
            ("आज", "आजचा", "आजचे", "आजची", "आजच्या", "आजचं", "aaj", "aajcha", "aajche", "aajchi", "aajchya"),
        ),
        PhraseRule(
            "mr.time.tomorrow",
            TOMORROW,
            (
                "उद्या",
                "उद्याचा",
                "उद्याचे",
                "उद्याची",
                "उद्याच्या",
                "उद्याचं",
                "udya",
                "udyacha",
                "udyache",
                "udyachi",
                "udyachya",
            ),
        ),
        *_marathi_weekdays(),
        PhraseRule(
            "mr.when.when",
            WHEN,
            ("कधी", "केव्हा", "केंव्हा", "किती वाजता", "kadhi", "kevha", "kevhaa", "kiti vajta", "kiti vajata"),
        ),
        PhraseRule(
            "mr.all.classes",
            ALL_CLASSES,
            (
                "सर्व वर्ग",
                "सगळे वर्ग",
                "सर्व वर्गांचे",
                "सर्व वर्गांच्या",
                "sarva varg",
                "sarv varg",
                "sagle varg",
                "sarva vargache",
            ),
        ),
    ),
    fillers=(
        "कोणता", "कोणती", "कोणते", "कोणत्या", "काय", "आहे", "आहेत", "असेल", "असते", "का", "माझा",
        "माझी", "माझे", "माझ्या", "माझं", "मला", "आम्हाला", "सांगा", "सांग", "दाखवा", "दाखव", "द्या",
        "दे", "कृपया", "मध्ये", "आणि", "व", "कुठे", "सर", "मॅडम", "बाई", "नमस्कार", "धन्यवाद", "maza",
        "majha", "majhe", "maze", "mazha", "mazhe", "majhi", "mazi", "majhya", "mazya", "mala", "amhala",
        "ahe", "aahe", "aahet", "ahet", "ase", "asel", "ka", "kay", "konta", "konti", "konte", "kontya",
        "kon", "sang", "sanga", "dakhva", "dakhav", "dya", "de", "kripaya", "krupaya", "madhe", "madhye",
        "ani", "aani", "va", "kuthe", "sir", "madam", "mam",
    ),
)

LANGUAGE_TABLES: Final[tuple[LanguageTable, ...]] = (ENGLISH, MARATHI)
