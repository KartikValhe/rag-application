"""
app/services/document_text.py
─────────────────────────────
Text rules shared by the sandboxed document child (which cleans what it extracts) and the parent
(which refuses anything from the child that is not clean). Step 5b-3, design A3 step 3.

A cleaned block of text satisfies, and `is_clean_text` checks exactly this:
  - NFC normalized;
  - no control character (C0, DEL, C1) and no lone surrogate;
  - no format character (every character of Unicode category Cf: U+200B, U+200E/F, U+202A-E,
    U+2066-9, U+2060-4, U+FEFF, U+00AD, Arabic number signs, tag characters ...), no combining
    grapheme joiner, Hangul filler, variation selector or noncharacter (see _INVISIBLE). The two joiners U+200D (ZWJ) and U+200C (ZWNJ)
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

# Invisible characters that are ALWAYS dropped (review of 5b-3, finding 7). The two joiners U+200C and
# U+200D are handled apart (a joiner survives only right after a Devanagari character).
#   - every character of Unicode category Cf (format) except the joiners: soft hyphen, Arabic number
#     signs, zero width space, directional marks and isolates, word joiner and invisible operators,
#     BOM, interlinear annotation, Kaithi, Egyptian and musical format controls, tag characters ...;
#   - invisible characters of other categories that carry no text and are used to hide or fingerprint
#     text: combining grapheme joiner U+034F, the Hangul fillers (U+115F, U+1160, U+3164, U+FFA0),
#     variation selectors (U+FE00-FE0F and U+E0100-E01EF), the Mongolian free variation selectors
#     (U+180B-180D) and the noncharacters U+FFFE and U+FFFF.
# The ranges were generated from Python 3.12's Unicode 15.0 data; tests/test_review_5b3_text.py checks
# EVERY Cf character of the running Python, so a newer Unicode version that adds one fails that test.
# Kept on purpose: Devanagari combining marks (matras, virama, nukta, anusvara ...), the danda and the
# Devanagari digits.
_INVISIBLE = (
    "\u00ad"  # soft hyphen
    "\u034f"  # combining grapheme joiner
    "\u0600-\u0605"  # Arabic number signs
    "\u061c"  # Arabic letter mark
    "\u06dd"  # Arabic end of ayah
    "\u070f"  # Syriac abbreviation mark
    "\u0890-\u0891"  # Arabic pound / piastre mark above
    "\u08e2"  # Arabic disputed end of ayah
    "\u115f-\u1160"  # Hangul choseong / jungseong fillers
    "\u180b-\u180e"  # Mongolian free variation selectors and vowel separator
    "\u200b"  # zero width space
    "\u200e-\u200f"  # left-to-right / right-to-left marks
    "\u202a-\u202e"  # bidi embeddings and overrides
    "\u2060-\u2064"  # word joiner and invisible operators
    "\u2066-\u206f"  # bidi isolates and deprecated format characters
    "\u3164"  # Hangul filler
    "\ufe00-\ufe0f"  # variation selectors 1-16
    "\ufeff"  # BOM / zero width no-break space
    "\uffa0"  # halfwidth Hangul filler
    "\ufff9-\ufffb"  # interlinear annotation
    "\ufffe-\uffff"  # noncharacters
    "\U000110bd\U000110cd"  # Kaithi number signs
    "\U00013430-\U0001343f"  # Egyptian hieroglyph format controls
    "\U0001bca0-\U0001bca3"  # shorthand format controls
    "\U0001d173-\U0001d17a"  # musical format controls
    "\U000e0001\U000e0020-\U000e007f"  # language tag and tag characters (invisible "ASCII smuggling")
    "\U000e0100-\U000e01ef"  # variation selectors 17-256
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


# ── The cut-point rule (shared by the parser's block splitter and the chunker) ──

JOINERS = "‌‍"  # ZWNJ, ZWJ
VIRAMA = "्"


def is_unsafe_cut(text: str, cut: int) -> bool:
    """True when cutting `text` into text[:cut] and text[cut:] would split a conjunct: the second piece
    would start with a joiner or a combining mark, or the first would end with a joiner or a virama.
    (A piece that starts with a joiner is not clean text: the parent would refuse the whole document.)
    `cut` must be inside the text (0 < cut < len(text))."""
    after = text[cut]
    before = text[cut - 1]
    return (
        after in JOINERS
        or unicodedata.category(after).startswith("M")
        or before in JOINERS
        or before == VIRAMA
    )


# ── Scripts and character counts ─────────────────────────────────────────────

SCRIPT_GUESSES = ("latn", "deva", "mixed")
# Devanagari shares of the Devanagari + Latin letters of a document
DEVA_SHARE_DEVA = 0.9  # at least this much: deva
DEVA_SHARE_LATN = 0.1  # at most this much: latn; in between: mixed


def is_devanagari(ch: str) -> bool:
    cp = ord(ch)
    return 0x0900 <= cp <= 0x097F or 0xA8E0 <= cp <= 0xA8FF


SYMBOL_FONT_START = 0xF000
SYMBOL_FONT_END = 0xF0FF


def is_symbol_font(ch: str) -> bool:
    """U+F000-U+F0FF: where Word and PDF producers put the characters of the Symbol and Wingdings fonts
    (bullets such as U+F0B7, Greek letters and operators of equations). They are real content, not garbled
    text, so the unreadable_text gate neither counts them as garbage nor as letters (review of 5b-3,
    finding 11). Legacy Devanagari fonts that use this range are therefore not caught by that range, and
    legacy fonts whose codes are plain ASCII ("Hkkjr dk lafo/kku") are not detected by this gate at all
    (backlog B50)."""
    return SYMBOL_FONT_START <= ord(ch) <= SYMBOL_FONT_END


def is_private_use(ch: str) -> bool:
    """A Private Use Area code point that is evidence of garbled text: U+E000-U+F8FF without the
    Symbol-font range."""
    return 0xE000 <= ord(ch) <= 0xF8FF and not is_symbol_font(ch)


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
