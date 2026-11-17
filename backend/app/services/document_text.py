"""
app/services/document_text.py
─────────────────────────────
Text rules shared by the sandboxed document child (which cleans what it extracts) and the parent
(which refuses anything from the child that is not clean). Step 5b-3, design A3 step 3.

A cleaned block of text satisfies, and `is_clean_text` checks exactly this:
  - NFC normalized;
  - no control character (C0, DEL, C1) and no lone surrogate;
  - no zero-width, bidi or other invisible format character (U+200B, U+200E/F, U+202A-E, U+2066-9,
    U+2060-4, U+FEFF, U+00AD, tag characters ...). The two joiners U+200D (ZWJ) and U+200C (ZWNJ)
    are kept ONLY directly after a Devanagari character, because they decide how a conjunct is
    drawn (क्‍ष); anywhere else they are invisible noise;
  - the only whitespace is a single ASCII space between words (newlines, tabs, no-break and other
    Unicode spaces become one space), and no leading or trailing space.
The Devanagari danda (U+0964) and double danda (U+0965) and all other punctuation are left alone.

Also here: the script guess and the character counts the quality gates use (document_parse_child).
This module imports only the standard library and app.core.upload_security (already loaded by the
child for the upload re-check); it must never import app.core.config.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from app.core.upload_security import clean_plain_text

# Invisible characters that are always dropped (the joiners U+200C and U+200D are handled apart).
_INVISIBLE = (
    "­"  # soft hyphen
    "؜"  # Arabic letter mark (bidi)
    "᠎"  # Mongolian vowel separator
    "​"  # zero width space
    "‎‏"  # left-to-right / right-to-left marks
    "‪-‮"  # bidi embeddings and overrides
    "⁠-⁤"  # word joiner and invisible operators
    "⁦-⁯"  # bidi isolates and deprecated format characters
    "﻿"  # BOM / zero width no-break space
    "￹-￻"  # interlinear annotation
    "\U000e0000-\U000e007f"  # tag characters (invisible "ASCII smuggling")
)
_C1_AND_SURROGATES = "\x80-\x84\x86-\x9f\ud800-\udfff"  # U+0085 (NEL) is whitespace and becomes a space

_DROP_RE = re.compile(f"[{_INVISIBLE}{_C1_AND_SURROGATES}]")
# A joiner not directly after a Devanagari character
_STRAY_JOINER_RE = re.compile("(?<![ऀ-ॿ])[‌‍]")
_ODD_SPACE_RE = re.compile(r"[^\S ]")  # any whitespace except the plain space
_SPACES_RE = re.compile(" {2,}")

# What must NOT be in a clean block (the parent's check)
_NOT_CLEAN_RE = re.compile(
    f"[\x00-\x1f\x7f-\x9f{_INVISIBLE}\ud800-\udfff]|{_STRAY_JOINER_RE.pattern}|[^\\S ]| {{2}}"
)


def clean_text(value: str) -> str:
    """
    One cleaned block: control, zero-width and bidi characters dropped, all whitespace turned into
    single spaces, NFC, trimmed. Returns "" when nothing is left.
    """
    # Whitespace first: python-pptx gives a line break as \x0b, which clean_plain_text would delete
    # and so glue two words together
    text = _ODD_SPACE_RE.sub(" ", value)
    text = clean_plain_text(text)  # the remaining C0 controls and DEL
    text = _DROP_RE.sub("", text)
    # NFC reorders combining marks, which can move a joiner away from its Devanagari letter, and
    # removing a joiner can make two marks adjacent that NFC must reorder: repeat until stable, and
    # as a last resort drop every joiner (text without joiners stays NFC after one pass).
    for _ in range(4):
        cleaned = unicodedata.normalize("NFC", _STRAY_JOINER_RE.sub("", text))
        if cleaned == text:
            break
        text = cleaned
    else:
        text = unicodedata.normalize("NFC", text.replace("‌", "").replace("‍", ""))
    return _SPACES_RE.sub(" ", text).strip(" ")


def is_clean_text(value: str) -> bool:
    """True when `value` is non-empty and already exactly what clean_text would produce."""
    if not value or value[0] == " " or value[-1] == " ":
        return False
    if _NOT_CLEAN_RE.search(value) is not None:
        return False
    return unicodedata.is_normalized("NFC", value)


# ── Scripts and character counts ─────────────────────────────────────────────

SCRIPT_GUESSES = ("latn", "deva", "mixed")
# Devanagari shares of the Devanagari + Latin letters of a document
DEVA_SHARE_DEVA = 0.9  # at least this much: deva
DEVA_SHARE_LATN = 0.1  # at most this much: latn; in between: mixed


def is_devanagari(ch: str) -> bool:
    cp = ord(ch)
    return 0x0900 <= cp <= 0x097F or 0xA8E0 <= cp <= 0xA8FF


def is_private_use(ch: str) -> bool:
    return 0xE000 <= ord(ch) <= 0xF8FF


# Latin-1 characters that are normal in English and mathematical text and so say nothing about a
# legacy-font mojibake: NBSP, section and copyright signs, degree, plus-minus, superscripts, micro,
# pilcrow, middle dot, guillemets, multiplication and division signs.
_LATIN1_NORMAL = frozenset(" ¡§©«®°±²³µ¶·¹»¿×÷")


def is_latin1_suspect(ch: str) -> bool:
    """A Latin-1 Supplement character that legacy Devanagari fonts use and plain text hardly ever does."""
    return "¡" <= ch <= "ÿ" and ch not in _LATIN1_NORMAL


@dataclass(frozen=True)
class CharCounts:
    """Letter statistics of a text. `letters` counts alphabetic characters plus private-use ones."""

    deva: int = 0
    latn: int = 0
    private_use: int = 0
    letters: int = 0
    latin1_suspect: int = 0
    non_space: int = 0

    def __add__(self, other: "CharCounts") -> "CharCounts":
        return CharCounts(
            self.deva + other.deva,
            self.latn + other.latn,
            self.private_use + other.private_use,
            self.letters + other.letters,
            self.latin1_suspect + other.latin1_suspect,
            self.non_space + other.non_space,
        )


def count_chars(text: str) -> CharCounts:
    """Letter statistics of `text` (counts distinct characters once, so a 2 MB text stays fast)."""
    deva = latn = pua = letters = suspect = non_space = 0
    for ch, n in Counter(text).items():
        if ch.isspace():
            continue
        non_space += n
        if is_private_use(ch):
            pua += n
            letters += n
        elif ch.isalpha():
            letters += n
            if is_devanagari(ch):
                deva += n
            elif ch < "ɐ" or "Ḁ" <= ch <= "ỿ":  # Latin, Latin-1, Latin Extended A/B
                latn += n
            if is_latin1_suspect(ch):
                suspect += n
        elif is_latin1_suspect(ch):
            suspect += n
        elif is_devanagari(ch):
            deva += n  # Devanagari signs (matras, virama) are not "alpha" for str.isalpha
    return CharCounts(deva, latn, pua, letters, suspect, non_space)


def guess_script(counts: CharCounts) -> str:
    """latn, deva or mixed, from the share of Devanagari among the Devanagari and Latin letters."""
    total = counts.deva + counts.latn
    if total == 0:
        return "latn"
    share = counts.deva / total
    if share >= DEVA_SHARE_DEVA:
        return "deva"
    if share <= DEVA_SHARE_LATN:
        return "latn"
    return "mixed"
