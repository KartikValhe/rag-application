"""
app/services/chat_language.py
─────────────────────────────
Which language a chat question is written in (plan 5.7). Step 6a-7.

    detect(text, default_language) -> "en" | "mr" | "hi"

  1. Count letters (Unicode categories L and M; digits, spaces, punctuation and the danda do not count).
  2. A Devanagari share of those letters ABOVE 30 % means Marathi or Hindi; anything else is "en".
     Romanised Marathi ("maza next lecture kadhi aahe") is Latin text, so it is "en" (the model is still told to
     answer in the language of the question).
  3. For Devanagari text the function-word lists decide: more Marathi hits gives "mr", more Hindi hits gives
     "hi", and a tie (including no hits at all) gives the tenant's default language.

Pure: no database, network, clock, random or file access, and no import outside the standard library. Total:
any string gives one of the three codes. Only the first MAX_SCAN_CHARS characters are read, so a huge text
cannot make it slow.

The word lists are matching data, not user-facing text. A word that both languages use ("का", "की", "हो", "तो")
is in neither list.
"""

from __future__ import annotations

import unicodedata
from typing import Final, Literal

Language = Literal["en", "mr", "hi"]
LANGUAGES: Final[tuple[Language, ...]] = ("en", "mr", "hi")

DEVANAGARI_SHARE_LIMIT: Final = 0.30
MAX_SCAN_CHARS: Final = 2000

# Marathi function words (NFC). Marathi attaches many endings to the word before it, so only whole words that
# stand alone are listed.
MARATHI_WORDS: Final[frozenset[str]] = frozenset(
    {
        "आहे",
        "आहेत",
        "काय",
        "कोणता",
        "कोणती",
        "कोणते",
        "कोणत्या",
        "च्या",
        "मध्ये",
        "आणि",
        "मला",
        "माझा",
        "माझी",
        "माझे",
        "माझ्या",
        "कधी",
        "केव्हा",
        "नाही",
        "साठी",
        "पासून",
        "होते",
        "होती",
        "होता",
        "आम्ही",
        "आपण",
        "म्हणजे",
        "कसे",
        "कसा",
        "कशी",
        "किती",
        "सांगा",
        "तर",
    }
)

# Hindi function words (NFC).
HINDI_WORDS: Final[frozenset[str]] = frozenset(
    {
        "है",
        "हैं",
        "क्या",
        "कौन",
        "के",
        "में",
        "और",
        "मुझे",
        "मेरा",
        "मेरी",
        "मेरे",
        "कब",
        "से",
        "पर",
        "नहीं",
        "था",
        "थी",
        "थे",
        "हूँ",
        "कि",
        "यह",
        "वह",
        "भी",
        "कैसे",
        "क्यों",
        "कितना",
        "कितने",
        "कितनी",
        "लिए",
        "सकता",
        "सकती",
        "चाहिए",
        "कहाँ",
        "कहां",
        "क्योंकि",
        "तक",
    }
)


def _is_devanagari(code: int) -> bool:
    return 0x0900 <= code <= 0x097F or 0xA8E0 <= code <= 0xA8FF


def _is_word_character(char: str) -> bool:
    """Part of a Devanagari word: a letter or combining mark of the block (not a digit, danda or sign)."""
    return _is_devanagari(ord(char)) and unicodedata.category(char)[0] in "LM"


def _valid_default(default_language: str) -> Language:
    if default_language == "mr":
        return "mr"
    if default_language == "hi":
        return "hi"
    return "en"


def detect(text: str, default_language: str) -> Language:
    """The language of `text`: "mr" or "hi" for Devanagari text, otherwise "en". `default_language` (the tenant's)
    decides a tie; a value that is not en, mr or hi counts as "en"."""
    default = _valid_default(default_language)
    scanned = unicodedata.normalize("NFC", text[:MAX_SCAN_CHARS]) if isinstance(text, str) else ""

    letters = 0
    devanagari = 0
    words: list[str] = []
    current: list[str] = []
    for char in scanned:
        category = unicodedata.category(char)
        if category == "Cf":  # zero-width and bidi characters are invisible: they neither count nor split a word
            continue
        if category[0] in "LM":
            letters += 1
            if _is_devanagari(ord(char)):
                devanagari += 1
        if _is_word_character(char):
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))

    if letters == 0 or devanagari / letters <= DEVANAGARI_SHARE_LIMIT:
        return "en"

    marathi = sum(1 for word in words if word in MARATHI_WORDS)
    hindi = sum(1 for word in words if word in HINDI_WORDS)
    if marathi > hindi:
        return "mr"
    if hindi > marathi:
        return "hi"
    return default
