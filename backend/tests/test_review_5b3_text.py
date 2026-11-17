"""
Review of step 5b-3, finding 7: clean_text drops EVERY Unicode format character (category Cf) except the
joiners that legitimately occur inside Indic words (the existing rule: ZWJ/ZWNJ right after a Devanagari
character), plus the invisible characters of other categories that carry no text: combining grapheme joiner,
Hangul fillers, variation selectors, Mongolian free variation selectors and the noncharacters U+FFFE/FFFF.
Devanagari combining marks, the danda and Devanagari digits are text and stay.
"""

import sys
import unicodedata

import pytest

from app.services.document_text import clean_text, is_clean_text

ZWJ, ZWNJ = "\u200d", "\u200c"

# Words that must survive unchanged (they are NFC already, which the assertion below checks)
HINDI_WORDS = [
    "भारत",
    "संविधान।",
    "क्षत्रिय",
    "ज्ञान",
    "विद्यार्थी",
    "दुःख",  # visarga
    "अँग्रेज़ी",  # chandrabindu, nukta
    "कॉलेज",  # candra o matra
    "१२३४५६७८९०",  # Devanagari digits
    "॥ श्री ॥",  # double danda
    "क्" + ZWJ + "ष",  # explicit half-form request: ZWJ after a virama
    "क्" + ZWNJ + "ष",  # explicit conjunct break: ZWNJ after a virama
]
MARATHI_WORDS = [
    "महाराष्ट्र",
    "ऱ्" + ZWJ + "या",  # eyelash ra: ZWJ is part of the spelling
    "विद्यार्थ्यांसाठी",
    "मंडळ",  # the Marathi retroflex lateral
    "अभ्यासक्रम॥",
    "ऍडमिशन",
    "सूचना।",
]


@pytest.mark.parametrize("word", HINDI_WORDS + MARATHI_WORDS)
def test_hindi_and_marathi_words_survive_unchanged(word: str) -> None:
    word = unicodedata.normalize("NFC", word)  # the text is NFC by contract; the letters themselves must not change
    assert clean_text(word) == word
    assert is_clean_text(word)
    assert clean_text(f"abc {word} xyz") == f"abc {word} xyz"


def _format_characters() -> list[str]:
    return [chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf" and chr(c) not in (ZWJ, ZWNJ)]


def test_every_format_character_except_the_joiners_is_dropped() -> None:
    """Exhaustive over this Python's Unicode data: soft hyphen, Arabic number signs, interlinear
    annotation, musical and Egyptian format controls, tag characters, ..."""
    kept = [f"U+{ord(ch):04X}" for ch in _format_characters() if clean_text(f"अ{ch}ब") != "अब"]
    assert kept == []


@pytest.mark.parametrize(
    "ch",
    [
        "؀",  # ARABIC NUMBER SIGN
        "܏",  # SYRIAC ABBREVIATION MARK
        "࢐"[:0] + "࣢",  # ARABIC DISPUTED END OF AYAH
        "\U000110bd",  # KAITHI NUMBER SIGN
        "\U0001d173",  # MUSICAL SYMBOL BEGIN BEAM
        "\U000e0001",  # LANGUAGE TAG
        "\U000e0041",  # TAG LATIN CAPITAL LETTER A
        "\u00ad",  # SOFT HYPHEN
        "\u2060",  # WORD JOINER
        "\ufeff",  # BOM
    ],
)
def test_format_characters_are_dropped_between_letters(ch: str) -> None:
    assert clean_text(f"a{ch}b") == "ab"
    assert not is_clean_text(f"a{ch}b")


@pytest.mark.parametrize(
    ("ch", "name"),
    [
        ("\u034f", "combining grapheme joiner"),
        ("\u115f", "Hangul choseong filler"),
        ("\u1160", "Hangul jungseong filler"),
        ("\u3164", "Hangul filler"),
        ("\uffa0", "halfwidth Hangul filler"),
        ("\ufe00", "variation selector-1"),
        ("\ufe0f", "variation selector-16"),
        ("\U000e0100", "variation selector-17"),
        ("\U000e01ef", "variation selector-256"),
        ("\u180b", "Mongolian free variation selector one"),
        ("\u180c", "Mongolian free variation selector two"),
        ("\u180d", "Mongolian free variation selector three"),
        ("\ufffe", "noncharacter U+FFFE"),
        ("\uffff", "noncharacter U+FFFF"),
    ],
)
def test_invisible_non_format_characters_are_dropped(ch: str, name: str) -> None:
    assert clean_text(f"a{ch}b") == "ab", name
    assert clean_text(f"अ{ch}ब") == "अब", name
    assert not is_clean_text(f"a{ch}b"), name


def test_variation_selector_ranges_are_dropped_completely() -> None:
    for cp in list(range(0xFE00, 0xFE10)) + list(range(0xE0100, 0xE01F0)):
        assert clean_text(f"a{chr(cp)}b") == "ab", hex(cp)


def test_joiners_outside_devanagari_are_still_dropped() -> None:
    assert clean_text(f"a{ZWJ}b{ZWNJ}c") == "abc"
    assert clean_text(f"{ZWJ}क") == "क"


def test_a_lookalike_hidden_in_hindi_text_is_cleaned_without_touching_the_matras() -> None:
    word = "भ\u034fा\ufe0fर\u115fत"  # joiner, variation selector and filler hidden inside a word
    assert clean_text(word) == "भारत"

