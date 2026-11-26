"""
app/services/pii.py
───────────────────
PII masking of the text that goes to a model (docs/phase6-plan.md 9.5 item 1, design B2; step 6a-10a). Shared with
Phase 7. Pure: no database, no network.

The question is stored as the student typed it; the LLM receives a masked copy. Masked: e-mail addresses, Indian
mobile numbers (+91 optional) and any 10-digit phone number, 12-digit ID numbers (Aadhaar style, with or without
spaces or hyphens), and the caller's own e-mail address and name (the full name, and each part of it with at least
three characters, as a whole word, ignoring case). Digits include Devanagari digits. Placeholders are never restored.

This is a simple filter, not a guarantee: a number written in words, a name of somebody else or an address is not
caught. Phase 7 adds detection; the UI asks students not to type personal information (9.5 item 4).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final

EMAIL: Final = "[email]"
PHONE: Final = "[phone]"
ID_NUMBER: Final = "[id number]"
NAME: Final = "[name]"
MIN_NAME_PART_CHARS: Final = 3

# A word character in Latin or Devanagari (Devanagari vowel signs are not \w, so the whole block is listed).
_WORD = r"\w" + chr(0x0900) + "-" + chr(0x097F)
_EMAIL_RE: Final = re.compile(r"(?<![\w.+-])[\w.%+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}(?![\w-])")
_D = r"\d"  # str patterns: \d is every Unicode decimal digit (ASCII and Devanagari)
# not after "+": +91 and ten digits is a phone number, not an ID
_ID_RE: Final = re.compile(rf"(?<![{_D}+])(?<!{_D}[ -]){_D}{{4}}[ -]?{_D}{{4}}[ -]?{_D}{{4}}(?![ -]?{_D})")
_PHONE_RE: Final = re.compile(
    rf"(?<![{_D}+])(?<!{_D}[ -])(?:\+91[ -]?)?(?:{_D}{{10}}|{_D}{{5}}[ -]{_D}{{5}}|{_D}{{3}}[ -]{_D}{{3}}[ -]{_D}{{4}})"
    rf"(?![ -]?{_D})"
)


def _word_re(word: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![{_WORD}]){re.escape(word)}(?![{_WORD}])", re.IGNORECASE)


def mask(text: str, *, own_name: str | None = None, own_email: str | None = None) -> str:
    if own_email and own_email.strip():
        text = re.sub(re.escape(own_email.strip()), EMAIL, text, flags=re.IGNORECASE)
    text = _EMAIL_RE.sub(EMAIL, text)
    text = _ID_RE.sub(ID_NUMBER, text)
    text = _PHONE_RE.sub(PHONE, text)
    if own_name and own_name.strip():
        full = " ".join(own_name.split())
        text = _word_re(full).sub(NAME, text)
        for part in sorted(set(full.split()), key=len, reverse=True):
            if len(part) >= MIN_NAME_PART_CHARS:
                text = _word_re(part).sub(NAME, text)
    return text


def masker(*, own_name: str | None, own_email: str | None) -> Callable[[str], str]:
    """A mask bound to the caller's own name and e-mail (what the prompt builder takes)."""
    def apply(text: str) -> str:
        return mask(text, own_name=own_name, own_email=own_email)

    return apply
