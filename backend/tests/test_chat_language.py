"""
tests/test_chat_language.py
───────────────────────────
Step 6a-7 (plan 5.7): chat_language.detect(text, default_language).

A Devanagari share above 30 % of the letters gives "mr" or "hi" by function-word lists; a tie gives the tenant's
default language; everything else is "en" (Romanised Marathi is Latin text, so "en"). The module is pure.
"""

import ast
import random
import string
from pathlib import Path

import pytest

from app.services import chat_language
from app.services.chat_language import (
    DEVANAGARI_SHARE_LIMIT,
    HINDI_WORDS,
    MARATHI_WORDS,
    MAX_SCAN_CHARS,
    detect,
)

MODULE_PATH = Path(chat_language.__file__)

# ── Marathi, Hindi and English tables ─────────────────────────────────────────

MARATHI = [
    "आजचे वेळापत्रक काय आहे",
    "मला पुढचा तास कधी आहे",
    "माझा पुढचा तास कोणता आहे",
    "भौतिकशास्त्र म्हणजे काय",
    "उद्या कोणते तास आहेत",
    "परीक्षा कधी आहे",
    "गणिताच्या वर्गात काय शिकवले",
    "मला वेळापत्रक सांगा आणि खोली दाखवा",
    "आजचे लेक्चर कोणत्या खोलीत आहे",
    "माझी परीक्षा कोणत्या दिवशी आहे",
]

HINDI = [
    "मुझे अगला लेक्चर कब है यह बताओ",
    "क्या आज की कक्षा है",
    "आज का टाइमटेबल क्या है",
    "भौतिकी के नियम क्या हैं",
    "कौन सा विषय कल है",
    "मेरा पाठ्यक्रम कहाँ है और कब है",
    "परीक्षा कब है",
    "मेरे लिए नोट्स चाहिए",
    "यह लेक्चर नहीं है",
    "कक्षा में क्या पढ़ाया गया था",
]

ENGLISH = [
    "what is my next lecture",
    "when is the physics exam",
    "explain newton's second law",
    "room for improvement",
    "2 + 2 = ?",
    "pudhcha lecture kadhi aahe",  # Romanised Marathi is Latin text
    "maza next lecture kadhi aahe",
    "aajche velapatrak sang",
    "kay re bhava",
    "hello",
]


@pytest.mark.parametrize("text", MARATHI)
@pytest.mark.parametrize("default", ["en", "mr", "hi"])
def test_marathi_function_words_give_mr_whatever_the_default(text: str, default: str) -> None:
    assert detect(text, default) == "mr"


@pytest.mark.parametrize("text", HINDI)
@pytest.mark.parametrize("default", ["en", "mr", "hi"])
def test_hindi_function_words_give_hi_whatever_the_default(text: str, default: str) -> None:
    assert detect(text, default) == "hi"


@pytest.mark.parametrize("text", ENGLISH)
@pytest.mark.parametrize("default", ["en", "mr", "hi"])
def test_latin_text_is_en_even_when_the_tenant_default_is_marathi(text: str, default: str) -> None:
    assert detect(text, default) == "en"


# ── ties give the tenant default ──────────────────────────────────────────────


@pytest.mark.parametrize("default", ["en", "mr", "hi"])
@pytest.mark.parametrize("text", ["वेळापत्रक", "भौतिकशास्त्र गणित", "रसायनशास्त्र"])
def test_devanagari_text_without_function_words_gives_the_tenant_default(text: str, default: str) -> None:
    # no function word of either list: a tie, so the default decides (plan 5.7)
    assert detect(text, default) == default


def test_equal_marathi_and_hindi_hits_is_a_tie() -> None:
    # one Marathi word (काय) and one Hindi word (क्या): a tie
    assert detect("काय क्या", "mr") == "mr"
    assert detect("काय क्या", "hi") == "hi"
    assert detect("काय क्या", "en") == "en"


def test_one_more_hit_breaks_the_tie() -> None:
    assert detect("काय क्या आहे", "hi") == "mr"
    assert detect("काय क्या है", "mr") == "hi"


def test_an_unknown_default_is_treated_as_english() -> None:
    assert detect("वेळापत्रक", "fr") == "en"
    assert detect("वेळापत्रक", "") == "en"
    assert detect("hello", "xx") == "en"


# ── the 30 % border (strictly above) ──────────────────────────────────────────


def test_share_exactly_30_percent_is_not_above_the_limit() -> None:
    text = "अ" * 3 + "a" * 7  # 3 Devanagari letters of 10 = exactly 0.30
    assert DEVANAGARI_SHARE_LIMIT == 0.30
    assert detect(text, "mr") == "en"


def test_share_just_above_30_percent_goes_to_the_function_word_step() -> None:
    text = "अ" * 31 + "a" * 69  # 0.31
    assert detect(text, "mr") == "mr"  # no function words, tie, default mr
    assert detect(text, "hi") == "hi"


def test_mixed_script_text_counts_letters_only() -> None:
    # digits, spaces and punctuation are not letters: "कधी आहे" is 6 letters/marks vs 11 Latin letters = 0.35
    assert detect("next lecture कधी आहे", "en") == "mr"
    # many digits and symbols do not dilute the share
    assert detect("1234567890 !!!! ???? कधी आहे", "en") == "mr"
    assert detect("1234567890 !!!! ???? hello", "mr") == "en"


def test_devanagari_digits_and_danda_are_not_letters() -> None:
    assert detect("१२३४५६७८९० ।।।", "mr") == "en"  # no letters at all: "en"
    assert detect("१२३ hello", "mr") == "en"


def test_empty_and_symbol_only_text_is_en() -> None:
    for text in ["", " ", "\n\t", "???", "12345", "...", "🙂🙂", "​‎"]:
        assert detect(text, "mr") == "en"
        assert detect(text, "hi") == "en"


# ── the word lists ────────────────────────────────────────────────────────────


def test_word_lists_do_not_share_a_word() -> None:
    assert not (MARATHI_WORDS & HINDI_WORDS)


def test_word_lists_contain_the_words_the_plan_names() -> None:
    for word in ("आहे", "काय", "कोणता", "च्या"):
        assert word in MARATHI_WORDS
    for word in ("है", "क्या", "कौन", "के"):
        assert word in HINDI_WORDS


def test_word_lists_are_in_the_form_the_detector_compares() -> None:
    import unicodedata

    for word in MARATHI_WORDS | HINDI_WORDS:
        assert word == unicodedata.normalize("NFC", word)
        assert word == word.strip()
        assert " " not in word


def test_punctuation_around_a_function_word_does_not_hide_it() -> None:
    assert detect("आजचे वेळापत्रक काय आहे?", "hi") == "mr"
    assert detect("आजचे वेळापत्रक, काय आहे।", "hi") == "mr"
    assert detect("(क्या) आज ​ कक्षा है!", "mr") == "hi"


def test_zero_width_characters_inside_text_do_not_change_the_result() -> None:
    assert detect("काय​ आहे", "hi") == "mr"
    assert detect("‮काय आहे‬", "hi") == "mr"


# ── total and pure ────────────────────────────────────────────────────────────


def test_detect_never_raises_on_hostile_strings() -> None:
    rng = random.Random(20261125)
    pools = [
        string.printable,
        "".join(chr(c) for c in range(0x0900, 0x0980)),
        "".join(chr(c) for c in range(0xD800, 0xE000)),  # lone surrogates
        "‪‫‬‭‮⁦⁧⁨⁩​‌‍‎‏﻿",
        "".join(chr(c) for c in range(0x0000, 0x0100)),
    ]
    for _ in range(3000):
        pool = rng.choice(pools)
        n = rng.choice([0, 1, 2, 5, 40, 600])
        text = "".join(rng.choice(pool) for _ in range(n))
        assert detect(text, rng.choice(["en", "mr", "hi", "zz"])) in ("en", "mr", "hi")
    assert detect("क" * 1_000_000, "mr") == "mr"
    assert detect("a" * 1_000_000, "mr") == "en"


def test_detect_looks_only_at_a_bounded_prefix() -> None:
    # a huge text cannot make detect slow or change the result because of its tail
    head = "आजचे वेळापत्रक काय आहे "
    assert MAX_SCAN_CHARS == 2000
    padded = head.ljust(MAX_SCAN_CHARS)  # the words fill the first 2000 characters, the Latin tail starts after them
    assert detect(padded + "a" * 1_000_000, "hi") == "mr"
    assert detect(head + "a" * 1_000_000, "hi") == "en"  # a tail inside the bound still counts


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.add(("." * node.level) + (node.module or ""))
    return out


def test_chat_language_imports_only_the_standard_library_basics() -> None:
    allowed = {"__future__", "unicodedata", "typing"}
    found = _imports(MODULE_PATH)
    assert found <= allowed, f"unexpected imports: {sorted(found - allowed)}"
    forbidden_roots = {"sqlalchemy", "httpx", "redis", "app", "time", "random", "os", "pathlib", "asyncio", "socket"}
    assert not {name.split(".")[0] for name in found} & forbidden_roots


def test_chat_language_reads_no_clock_and_no_file() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    banned = {"now", "today", "utcnow", "time", "monotonic", "perf_counter", "random", "randint", "open", "read_text", "getenv"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            assert name not in banned, f"chat_language calls {name}"
