"""
Text cleaning, script guess and the quality-gate thresholds (step 5b-3, design A3 step 4).

Each threshold is tested on both sides: the value just inside passes, the value just outside fails.
The heuristics are written down in docs/api/phase5.md (section 9) and the 5b-3 record of the design.
"""

import random

import pytest

from app.services import document_gates as gates
from app.services.document_text import (
    CharCounts,
    clean_text,
    count_chars,
    guess_script,
    is_clean_text,
)

# ── clean_text ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("  Unit\t1:\n\nAlgebra  ", "Unit 1: Algebra"),
        ("line one\x0bline two", "line one line two"),  # python-pptx line break
        ("a\x00b\x1bc\x7fd", "abcd"),
        ("a\x85b", "a b"),  # NEL is a line break
        ("a b c d", "a b c d"),
        ("pay‮load", "payload"),
        ("in⁦vis⁩ible", "invisible"),
        ("zero​width﻿", "zerowidth"),
        ("soft­hyphen", "softhyphen"),
        ("tag\U000e0041\U000e0042s", "tags"),
        ("é", "é"),  # NFC
        ("क़", "क़"),  # NFC keeps nukta letters decomposed (composition exclusion)
        ("वाक्य। दूसरा॥", "वाक्य। दूसरा॥"),  # danda and double danda stay
        ("क्‍ष", "क्‍ष"),  # ZWJ after a Devanagari sign stays
        ("a‍b", "ab"),  # ZWJ anywhere else is dropped
        ("‌क", "क"),
        ("\x01 \x02", ""),
    ],
)
def test_clean_text(raw: str, clean: str) -> None:
    assert clean_text(raw) == clean


def test_clean_text_output_is_always_accepted_by_the_parent_check() -> None:
    """Fuzz: whatever goes in, what comes out is either empty or exactly what the parent accepts."""
    alphabet = (
        "abc XYZ\t\n\r\x00\x0b\x0c\x1f\x7f\x85\x9f ­؜​‌‍‎‪‮"
        "⁠⁦⁩﻿\ud800\U000e0020कषा्ि़।॥́e  é"
    )
    rng = random.Random(1234)
    for _ in range(3000):
        raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 25)))
        out = clean_text(raw)
        assert out == "" or is_clean_text(out), repr(raw)
        assert clean_text(out) == out  # idempotent


def test_is_clean_text_refuses_what_clean_text_would_change() -> None:
    for bad in ["", " a", "a ", "a  b", "a\nb", "a​b", "é", "a\x00", "a‍b"]:
        assert not is_clean_text(bad), repr(bad)
    assert is_clean_text("लघुगणक और घातांक।")


# ── Script guess (deva share >= 0.9: deva; <= 0.1: latn; else mixed) ────────


@pytest.mark.parametrize(
    ("deva", "latn", "expected"),
    [
        (90, 10, "deva"),  # exactly 0.9
        (89, 11, "mixed"),  # just under
        (10, 90, "latn"),  # exactly 0.1
        (11, 89, "mixed"),  # just over
        (0, 0, "latn"),  # no letters at all
        (50, 50, "mixed"),
    ],
)
def test_script_guess_thresholds(deva: int, latn: int, expected: str) -> None:
    assert guess_script(CharCounts(deva=deva, latn=latn)) == expected


def test_counts_on_real_text() -> None:
    hindi = count_chars("भारत का संविधान")
    marathi = count_chars("महाराष्ट्र राज्य शिक्षण मंडळ")
    english = count_chars("Linear equations")
    assert guess_script(hindi) == "deva" and guess_script(marathi) == "deva"
    assert guess_script(english) == "latn"
    assert guess_script(count_chars("Physics भौतिकशास्त्र Chemistry रसायनशास्त्र")) == "mixed"
    assert count_chars("ab").private_use == 2


# ── scanned_pdf: fewer than 20 non-space characters on 80 % or more of the pages ──


def test_scanned_pdf_character_threshold() -> None:
    gates.check_scanned_pdf([20] * 10)  # 20 is enough: not sparse
    with pytest.raises(gates.GateError) as exc:
        gates.check_scanned_pdf([19] * 10)
    assert exc.value.code == "scanned_pdf"


def test_scanned_pdf_page_share_threshold() -> None:
    with pytest.raises(gates.GateError):
        gates.check_scanned_pdf([0] * 8 + [500] * 2)  # exactly 80 % sparse
    gates.check_scanned_pdf([0] * 7 + [500] * 3)  # 70 %: passes
    with pytest.raises(gates.GateError):
        gates.check_scanned_pdf([0] * 4 + [500])  # 4 of 5 = 80 %
    gates.check_scanned_pdf([0] * 79 + [500] * 21)  # 79 %: passes


def test_scanned_pdf_with_no_pages_is_not_this_gate() -> None:
    gates.check_scanned_pdf([])  # no_text_found handles an empty result


# ── unreadable_text: private-use share of letters > 30 %, or Latin-1 runs > 30 % in Devanagari-targeted text ──


def test_private_use_threshold() -> None:
    gates.check_unreadable_text(CharCounts(private_use=30, letters=100, non_space=100), lang_hint=False)
    with pytest.raises(gates.GateError) as exc:
        gates.check_unreadable_text(CharCounts(private_use=31, letters=100, non_space=100), lang_hint=False)
    assert exc.value.code == "unreadable_text"


def test_latin1_threshold_applies_to_devanagari_targeted_text_only() -> None:
    at = CharCounts(latin1_suspect=30, letters=100, non_space=100)
    over = CharCounts(latin1_suspect=31, letters=100, non_space=100)
    gates.check_unreadable_text(at, lang_hint=True)
    with pytest.raises(gates.GateError):
        gates.check_unreadable_text(over, lang_hint=True)
    # Not targeted at Devanagari (no hi/mr metadata, no Devanagari letter): left alone
    gates.check_unreadable_text(CharCounts(latin1_suspect=80, letters=100, non_space=100), lang_hint=False)
    # A single Devanagari letter makes the text targeted
    with pytest.raises(gates.GateError):
        gates.check_unreadable_text(CharCounts(deva=1, latin1_suspect=40, letters=100, non_space=100), lang_hint=False)


def test_latin1_suspects_ignore_normal_math_and_typography() -> None:
    counts = count_chars("x² + y³ = 90° ± 5 × 3 ÷ 2 © § « »")
    assert counts.latin1_suspect == 0
    assert count_chars("Ãæ¼½ð").latin1_suspect == 5


def test_english_with_accents_is_far_below_the_latin1_threshold() -> None:
    counts = count_chars("Résumé of the café syllabus: naïve Bayes and Gödel's theorems, señor.")
    assert counts.latin1_suspect * 100 < 30 * counts.non_space
    gates.check_unreadable_text(counts, lang_hint=True)
