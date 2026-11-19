"""
app/services/injection_scan.py
──────────────────────────────
The prompt-injection tripwire (step 5d-2, review fixes 5d-2b; design A6; docs/phase5d-plan.md section 7).
Pure: no database, no network, no model. It FLAGS text and never changes or drops it.

    scan_text(text)                    codes found in one text
    scan_metadata(value)               a document title or file name: cleaned like chunk text, then scan_text
    scan_chunk(heading_path, content)  codes of one chunk: heading path + "\\n" + content
    scan_chunks(chunks)                one tuple of codes per chunk (the index stage runs this in a thread)
    find_matches(text)                 the first match of each code, as a span of the ORIGINAL text
    review_matches(heading, content)   per code: where it matched (heading_path or content) and an excerpt
    review_excerpt(content)            the content around its first match (the flags route's `excerpt`)

The scan copy (normalize_for_scan), never stored and never returned:
  1. the characters ingest cleaning always drops (document_text.invisible_code_points: every format character,
     variation selectors, fillers, C1 controls, lone surrogates) and the C0 controls are removed; Unicode tag
     characters (U+E0020-E007E, "ASCII smuggling") become the ASCII they hide;
  2. NFKD and casefold (full width, ligatures, mathematical letters and upper case make no difference);
  3. Cyrillic and Greek look-alikes of the Latin letters used by the rules become those letters;
  4. combining marks directly after a Latin letter are removed (accents, and the dot of "i" + U+0307 that
     casefold makes of "İ"); marks after any other letter, so every Devanagari vowel sign, virama and nukta,
     are never touched;
  5. NFKC, then every run of whitespace becomes one space, or one line break if it contains a line break.
Steps 1 and 5 are what clean_text does to chunk text, so a chunk, a title and a file name are scanned the same
way (titles and file names also go through clean_text itself: scan_metadata).

Codes (the rule table of plan section 7, in this order): instruction_override, role_markup, role_play,
prompt_extraction, model_directive, tool_call, link_directive, encoded_blob. Codes never contain a comma (the
index stage joins them with commas).

THIS IS A TRIPWIRE, NOT A GUARANTEE. Paraphrases, other languages, text in images, a phrase split across two
chunks and anything not foreseen pass unflagged, while text that is really ABOUT injection (a lesson that
quotes a jailbreak prompt) is flagged. A flag is a reason for a human to look; "not flagged" never means
"safe". The rules are narrowed so ordinary teaching text is not flagged (tests/fixtures/scan_corpus.py).

Linear time (the ReDoS rule of upload_security.py), shown by tests without a clock:
  - Every rule is a list of alternatives that each start with a literal "trigger" (ignore, <, http, पिछल ...).
    A trigger pattern (pure literals, no repetition) finds the next position where a trigger starts; the scan
    resumes one character after it, so the trigger searches cover disjoint parts of the text.
  - At each trigger position the rule is matched ANCHORED there and may read at most ANCHORED_SPAN characters
    (every repetition is bounded by MAX_REPEAT and no match is longer than MAX_MATCH_CHARS).
  - encoded_blob has no literal start; its pattern only starts at the beginning of a base64 run (a negative
    lookbehind) and reads at most 400 characters from there, so it is one ordinary search over the text.
Every match starts where one of its rule's triggers starts, so this finds exactly what a plain search would.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from app.services.document_text import clean_text, invisible_code_points

RULE_CODES: tuple[str, ...] = (
    "instruction_override",
    "role_markup",
    "role_play",
    "prompt_extraction",
    "model_directive",
    "tool_call",
    "link_directive",
    "encoded_blob",
)

MAX_REPEAT = 200  # the longest repetition any pattern may use
MAX_MATCH_CHARS = 512  # the longest match any pattern can make (checked by a test)
LOOKAROUND_REACH = 16  # how far a lookahead may read past the end of a match (one character today)
ANCHORED_SPAN = MAX_MATCH_CHARS + LOOKAROUND_REACH  # what one anchored match may read

# ── The scan copy ────────────────────────────────────────────────────────────

# Every format character (category Cf) of the Unicode version of Python 3.12, as ranges. invisible_code_points()
# covers them too (a test checks both against the running Python); these stay for the joiners U+200C/U+200D,
# which clean_text keeps after Devanagari but the scan copy always drops.
_FORMAT_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD), (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD), (0x070F, 0x070F),
    (0x0890, 0x0891), (0x08E2, 0x08E2), (0x180E, 0x180E), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x2064), (0x2066, 0x206F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0x110BD, 0x110BD),
    (0x110CD, 0x110CD), (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)
_C0_CONTROLS: tuple[tuple[int, int], ...] = ((0x00, 0x08), (0x0E, 0x1B), (0x7F, 0x7F))  # not the whitespace ones
_TAG_ASCII_FIRST, _TAG_ASCII_LAST = 0xE0020, 0xE007E  # tag characters that mirror printable ASCII

# Cyrillic, Greek and Latin look-alikes of the letters the English rules use (after casefold). Small on purpose:
# it covers the letters of the trigger words, not every confusable of Unicode.
_LOOKALIKES = {
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t", "у": "y",
    "х": "x", "ѕ": "s", "і": "i", "ј": "j", "ԁ": "d", "һ": "h", "ӏ": "l", "ԛ": "q", "ԝ": "w", "ү": "y", "ɡ": "g",
    # Greek
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u",
    "χ": "x", "γ": "y", "ω": "w", "ϲ": "c", "ϳ": "j",
    # Latin letters that NFKD and casefold leave alone
    "ı": "i", "ȷ": "j", "ɑ": "a", "ɩ": "i", "ɪ": "i", "ʟ": "l", "ɴ": "n", "ʀ": "r", "ꜱ": "s",
}


def _scan_map() -> dict[int, str | None]:
    table: dict[int, str | None] = {}
    for first, last in (*_FORMAT_RANGES, *invisible_code_points(), *_C0_CONTROLS):
        for cp in range(first, last + 1):
            table[cp] = None
    for cp in range(_TAG_ASCII_FIRST, _TAG_ASCII_LAST + 1):
        table[cp] = chr(cp - 0xE0000)
    return table


_SCAN_MAP = _scan_map()
_LOOKALIKE_MAP = {ord(k): v for k, v in _LOOKALIKES.items()}
# Combining marks (the diacritical blocks) directly after a Latin letter. Devanagari signs are not in these blocks.
_LATIN_MARKS = re.compile("(?<=[a-z])[̀-ͯ᪰-᫿᷀-᷿⃐-⃿︠-︯]+")
_SPACE_RUN = re.compile(r"[^\S\n]+")
_LINE_RUN = re.compile(r" ?\n\s*")


def normalize_for_scan(text: str) -> str:
    """The copy the rules run on (see the module docstring). Never stored, never returned to a client."""
    s = unicodedata.normalize("NFKD", text.translate(_SCAN_MAP)).casefold()
    s = unicodedata.normalize("NFKD", s).translate(_LOOKALIKE_MAP)
    s = unicodedata.normalize("NFKC", _LATIN_MARKS.sub("", s))
    return _LINE_RUN.sub("\n", _SPACE_RUN.sub(" ", s))


# ── Building blocks ──────────────────────────────────────────────────────────
# B / E: the edges of a Latin word.
B = r"(?<![a-z0-9])"
E = r"(?![a-z0-9])"
# One to four separators between two words: whitespace and the punctuation a file name or an evasion puts there
# (ignore_all_previous_instructions.pdf, ignore.all.previous, ignore/all/previous ...).
S = r"[\s_\-.,/:;]{1,4}"
DB = r"(?<![ऀ-ॣ०-ॿ])"  # the start of a Devanagari word (the danda separates words)
NUKTA = "़?"  # NFKC splits precomposed nukta letters (U+0958-095F) into letter + U+093C: both spellings match
QUOTES = ('"', "'", "“", "”", "‘", "’")
Q = "[" + "".join(QUOTES) + "]"
_DEVANAGARI_NEXT = r"(?=[ऀ-ॿ])"  # a cheap guard in front of a list of Devanagari words


def _any(*parts: str) -> str:
    return "(?:" + "|".join(parts) + ")"


_SPECIAL = set("[](){}?*+|^$.")


def _literal_prefix(head: str) -> str:
    """The literal characters every match of the regex `head` starts with (escapes such as \\[ count)."""
    out: list[str] = []
    i = 0
    while i < len(head):
        ch = head[i]
        if ch == "\\":
            if i + 1 >= len(head) or head[i + 1].isalnum():
                break  # a class such as \\s: not a literal
            literal, step = head[i + 1], 2
        elif ch in _SPECIAL:
            break
        else:
            literal, step = ch, 1
        if head[i + step : i + step + 1] in ("?", "*", "{"):
            break  # this character is optional or repeated
        out.append(literal)
        i += step
    if not out:
        raise ValueError(f"a rule alternative must start with a literal: {head!r}")
    return "".join(out)


class _Rule:
    """Alternatives of one code: (boundary, heads, rest) each, heads starting with a literal."""

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.triggers: set[str] = set()

    def add(self, boundary: str, heads: Sequence[str], rest: str = "") -> _Rule:
        self.sources.append(boundary + _any(*heads) + rest)
        self.triggers.update(_literal_prefix(h) for h in heads)
        return self

    def compile(self) -> tuple[re.Pattern[str], re.Pattern[str]]:
        literals = sorted(self.triggers, key=lambda t: (-len(t), t))
        return re.compile(_any(*self.sources)), re.compile("|".join(re.escape(t) for t in literals))


# Words that name a model, as heads (each starts with a literal) and as one group
_MODEL_HEADS: tuple[str, ...] = (
    "ai", r"a\.i\.", "assistant", "chatbot", "chat" + S + "bot", "bot", "language" + S + "model", "llm",
    "chatgpt", "chat" + S + "gpt", r"gpt(?:-?[0-9]{1,2}(?:\.[0-9])?)?", "claude", "gemini", "copilot", "bard",
    "llama",
)
_MODEL = _any(*_MODEL_HEADS)
# What a model is told it now is (role_play): a model word, or a state that removes its rules
_ROLE = _any(
    "dan", *_MODEL_HEADS, "ai" + S + "assistant", "unrestricted", "unfiltered", "uncensored", "jailbroken",
    "evil" + S + _any("ai", "assistant", "bot", "model"),
    "free" + S + _any("from", "of") + S + "(?:" + _any("all", "every", "any") + S + ")?"
    + _any("rules?", "restrictions?", "limits?", "filters?", "guidelines?"),
    "no" + S + "longer" + S + _any("bound", "restricted", "limited", "filtered"),
    "in" + S + _any("developer", "god", "dan", "unrestricted", "jailbreak", "jailbroken") + S + "mode",
)
_ARTICLE = "(?:" + _any("an?", "the", "my", "your") + S + ")?"
# Words that make "developer mode" or "jailbreak" about a model, not a phone (role_play)
_AI_CONTEXT_HEADS: tuple[str, ...] = (
    "ai", r"a\.i\.", "chatbot", "chat" + S + "bot", "bot", "assistant", "llm", "gpt", "chatgpt", "claude",
    "gemini", "copilot", "language" + S + "model", "model", "prompts?", "instructions?", "dan",
)
_AI_CONTEXT = _any(*_AI_CONTEXT_HEADS)
_STUDENT = _any(
    _any("a", "an", "the", "any") + S + _any("student", "user", "learner", "child", "pupil", "person"),
    _any("someone", "anyone", "anybody"),
)

# instruction_override ─────────────────────────────────────────────────────────
_EN_OVERRIDE_VERBS = ("ignore", "disregard", "forget", "skip", "bypass")
_EN_EARLIER = _any("previous", "prior", "above", "earlier", "preceding", "former", "original", "initial", "system", "old")
# An instruction or prompt noun is required ("forget the old rules of spelling" is ordinary text)
_EN_ORDERS = _any("instructions?", "prompts?", "directives?", "guidelines?", "programming", "system" + S + _any("prompt", "message"))
_FILLER = "(?:[a-z'’]{1,20}" + S + "){0,5}"  # up to five words of any kind in between
_HI_ORDERS = _any("निर्देश", "आदेश", "नियम", "हिदायत") + "(?:ों)?"
_HI_DISOBEY = _any(
    "अनदेखा", "अनदेखी", "नज" + NUKTA + "रअंदाज" + NUKTA, "भूल", "छोड" + NUKTA, "उपेक्षा", "मत" + S + "मानो",
    "न" + S + "मानें",
)
_MR_ORDERS = _any("सूचना", "सुचना", "निर्देश", "आदेश", "नियम") + "(?:ांकडे|ांना|ां|ाकडे|ना)?"
_MR_DISOBEY = "(?:कडे" + S + ")?" + _any("दुर्लक्ष", "विसर", "डावल", "पाळू" + S + "नका", "मानू" + S + "नका")

INSTRUCTION_OVERRIDE = (
    _Rule()
    .add(B, _EN_OVERRIDE_VERBS, S + _FILLER + _EN_EARLIER + S + _EN_ORDERS + E)
    # "rules" only when they are the reader's own: "forget your (previous) rules"
    .add(B, _EN_OVERRIDE_VERBS, S + _FILLER + "your" + S + "(?:" + _EN_EARLIER + S + ")?"
         + _any("rules?", "restrictions?", "guidelines?", "instructions?", "programming", "training", "prompts?") + E)
    .add(B, ("override",), S + "(?:all" + S + ")?" + _any("your", "the", "any", "previous", "prior", "system") + S
         + "(?:" + _any("previous", "prior", "system", "safety") + S + ")?"
         + _any("instructions?", "rules?", "programming", "guidelines?", "prompts?", "restrictions") + E)
    .add(B, _EN_OVERRIDE_VERBS, S + _any("everything", "all") + S + _any(
        "above", "before", "previously", "so" + S + "far", "you" + S + _any("were", "have" + S + "been") + S + "told") + E)
    .add(DB, ("पिछल[ेीा]", "पहले" + S + "के", "पहले" + S + "दिए" + S + "गए",
              "ऊपर" + S + _any("के", "दिए" + S + "गए", "लिखे"), "उपरोक्त", "पूर्व"),
         S + "(?:" + _any("सभी", "सारे", "सब") + S + ")?" + _HI_ORDERS + S + "(?:को" + S + ")?" + _HI_DISOBEY)
    .add(DB, ("मागील", "मागच्या", "आधीच्या", "आधीचे", "वरील", "वरच्या", "पूर्वीच्या", "याआधीच्या"),
         S + "(?:" + _any("सर्व", "सगळ्या", "सगळे") + S + ")?" + _MR_ORDERS + S + _MR_DISOBEY)
)

# role_markup ──────────────────────────────────────────────────────────────────
_SP = "[ \t]{0,3}"
ROLE_MARKUP = (
    _Rule()
    # chat-template tokens, also with spaces inside: <|im_start|>, < |im_start| >, <| system |>
    .add("", ("<" + _SP + r"\|",), _SP + _any(
        "im_start", "im_end", "im_sep", "system", "user", "assistant", "endoftext", "eot_id", "start_header_id",
        "end_header_id", "begin_of_text") + _SP + r"\|" + _SP + ">")
    .add("", (r"\[/?inst\]", r"<</?sys>>"))
    # "### system", and the Alpaca markers "### Instruction:" / "### Response:" (not a heading "## User guide")
    .add(r"(?<![^\s])", ("##",), r"#{0,4}[ \t]{0,4}" + _any("system" + E, "instruction[ \t]{0,2}:", "response[ \t]{0,2}:"))
    # "system:" at the very start of a line ("Assistant:" and "User:" are ordinary speaker labels)
    .add(r"(?<![^\n])", ("system",), r"[ \t]{0,4}:")
)

# role_play ────────────────────────────────────────────────────────────────────
# A model-ish role must follow ("you are now ready", "pretend to be a shopkeeper" are ordinary text)
_FROM_NOW_ON_ORDERS = _any(
    "are" + S + _ARTICLE + _ROLE,
    _any("will", "shall") + S + "be" + S + _ARTICLE + _ROLE,
    _any("will", "must", "shall", "should", "can") + S + _any(
        "ignore", "disregard", "bypass", "never" + S + "refuse", "not" + S + "refuse",
        _any("answer", "respond", "reply") + S + "without", "act" + S + "as" + S + _ARTICLE + _ROLE),
)
_HI_ROLE = _any("एआई", r"ए\.आई\.", "चैटबॉट", "चैटबोट", "बॉट", "डैन", "ai", "dan", "असीमित", "अनियंत्रित", "जेलब्रेक")
_MR_ROLE = _any("एआय", "एआई", "चॅटबॉट", "चॅटबोट", "बॉट", "डॅन", "ai", "dan", "अमर्याद", "जेलब्रेक")
_JAILBREAK = "jailbr" + _any("eak(?:s|ing|ed)?", "oken")
ROLE_PLAY = (
    _Rule()
    .add(B, ("you",), _any("['’]?re", S + "are") + S + "now" + S + _ARTICLE + _ROLE + E)
    .add(B, ("from",), S + "now" + S + "on" + S + "you" + S + _FROM_NOW_ON_ORDERS + E)
    .add(B, ("act",), S + "as" + S + _ARTICLE + _ROLE + E)
    .add(B, ("pretend",), S + _any("to" + S + "be", "you" + S + "are", "you['’]re", "that" + S + "you" + S + "are")
         + S + _ARTICLE + _ROLE + E)
    # "developer mode" and "jailbreak" only next to a word about a model or its prompt (not a phone's)
    .add(B, ("developer",), S + "mode" + E + r"[^\n]{0,60}?" + B + _AI_CONTEXT + E)
    .add(B, ("jailbr",), _JAILBREAK[len("jailbr"):] + E + r"[^\n]{0,40}?" + B + _AI_CONTEXT + E)
    .add(B, _AI_CONTEXT_HEADS, E + r"[^\n]{0,60}?" + B + _any("developer" + S + "mode", _JAILBREAK) + E)
    .add(B, ("do",), S + "anything" + S + "now" + E)
    .add(DB, ("अब",), S + "से" + S + _any("तुम", "तू") + S + "(?:एक" + S + ")?" + _HI_ROLE)
    .add(DB, ("आतापासून",), S + _any("तू", "तुम्ही") + S + "(?:एक" + S + ")?" + _MR_ROLE)
)

# prompt_extraction ────────────────────────────────────────────────────────────
PROMPT_EXTRACTION = (
    _Rule()
    .add(B, ("reveal", "show", "print", "repeat", "display", "output", "tell", "give", "leak", "dump", "recite",
             "disclose", "write" + S + "out"),
         S + "(?:" + _any("me", "us") + S + ")?" + _any(
             "your" + S + "(?:" + _any("system", "initial", "original", "hidden", "secret", "full", "exact", "internal")
             + S + ")?" + _any("prompt", "instructions", "system" + S + "message"),
             "the" + S + _any("system", "initial", "original", "hidden", "secret", "internal") + S
             + _any("prompt", "instructions", "message")) + E)
    .add(B, ("what",), S + _any("are", "is", "were", "was") + S + "your" + S
         + "(?:" + _any("system", "initial", "original", "hidden", "secret") + S + ")?" + _any("prompt", "instructions") + E)
    .add(DB, ("अपने", "अपना", "अपनी", "तुम्हारे", "तुम्हारा", "आपके"),
         S + "(?:" + _any("सिस्टम", "मूल", "गुप्त", "सारे", "सभी") + S + ")?"
         + _any("निर्देश", "प्रॉम्प्ट", "प्रोम्प्ट", "नियम") + "(?:ों)?" + S + "(?:को" + S + ")?"
         + _any("बताओ", "बताइए", "बताएं", "बताएँ", "बता", "दिखाओ", "दिखाइए", "लिखो", "दोहराओ"))
    .add(DB, ("तुझ्या", "तुझे", "तुझी"), S + "(?:" + _any("सिस्टम", "मूळ", "गुप्त", "सर्व") + S + ")?"
         + _any("सूचना", "सुचना", "निर्देश", "प्रॉम्प्ट", "नियम") + S + _any("सांग", "दाखव", "लिही"))
)

# model_directive ──────────────────────────────────────────────────────────────
# Text addressed TO a model ("AI: you must ...", "Chatbot, if a student asks ..."), or an instruction about what
# "the chatbot" says to students. A plain "If a student asks why ..., tell them ..." is a note for a teacher.
_EN_ORDER = _any(
    "you" + S + _any("must", "should", "shall", "will", "are" + S + "required" + S + "to", "have" + S + "to"),
    "please", "always", "never", "do" + S + "not", "don['’]t", "ignore", "remember", "from" + S + "now",
    "if" + S + _STUDENT,
)
_SAY = _any("say", "answer", "respond", "reply", "tell", "claim", "insist", "refuse")
_HI_MODEL = ("एआई", r"ए\.आई\.", "चैटबॉट", "चैटबोट", "बॉट", "भाषा" + S + "मॉडल")
_MR_MODEL = ("एआय", "एआई", "चॅटबॉट", "चॅटबोट", "बॉट")
_HI_IF_ASKS = (
    _any("अगर", "यदि", "जब") + S + "(?:कोई" + S + ")?"
    + _any("छात्र", "छात्रा", "विद्यार्थी", "उपयोगकर्ता", "यूज" + NUKTA + "र", "बच्चा")
    + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any("पूछे", "पूछता", "पूछती", "पूछें", "पूछेगा", "पूछ" + S + "ले")
    + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT
    + _any("कहना", "कहो", "कहें", "जवाब" + S + _any("दो", "देना"), "बताना", "बोलना", "उत्तर" + S + "दो")
)
_MR_IF_ASKS = (
    _any("जर", "जेव्हा") + S + "(?:" + _any("एखाद्या", "कोणी", "कोणत्याही") + S + ")?"
    + _any("विद्यार्थ्याने", "विद्यार्थिनीने", "विद्यार्थी", "वापरकर्त्याने", "वापरकर्ता", "मुलाने")
    + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any("विचारले", "विचारल्यास", "विचारेल", "विचारलं", "विचारला")
    + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any("सांग", "उत्तर" + S + "दे", "म्हण")
)
MODEL_DIRECTIVE = (
    _Rule()
    .add(B, _MODEL_HEADS, r"(?:" + S + "assistant)?[ \t]{0,2}[,:][ \t]{0,4}" + _EN_ORDER + E)
    .add(B, ("if",), S + _STUDENT + S + "asks" + E + r"[^\n]{0,80}?" + B + "(?:the" + S + ")?" + _MODEL + E
         + r"[^\n]{0,30}?" + B + _SAY + E)
    .add(DB, _HI_MODEL, r"[ \t]{0,2}[,:][ \t]{0,4}" + _any("तुम्हें", "तुम", "आपको", "आप", "हमेशा", "कभी", "कृपया"))
    .add(DB, _HI_MODEL, r"[ \t]{0,2}[,:]?[ \t]{0,4}" + _HI_IF_ASKS)
    .add(DB, _MR_MODEL, r"[ \t]{0,2}[,:][ \t]{0,4}" + _any("तू", "तुम्ही", "तुला", "तुम्हाला", "नेहमी", "कधीही", "कृपया"))
    .add(DB, _MR_MODEL, r"[ \t]{0,2}[,:]?[ \t]{0,4}" + _MR_IF_ASKS)
)

# tool_call ────────────────────────────────────────────────────────────────────
TOOL_CALL = (
    _Rule()
    .add("", ("</?",), _any("tool_calls?", "tool_use", "tool_result", "function_calls?", "functioncall") + r"(?![a-z0-9_])")
    .add("", QUOTES, _any("function_call", "tool_calls?", "tool_use", "tool_choice") + Q + r"[ \t]{0,4}:")
    .add("", (r"\{",), r"\s{0,8}" + Q + "name" + Q + r"[ \t]{0,4}:[ \t]{0,4}" + Q
         + "[^" + "".join(QUOTES) + r"\n]{1,64}" + Q + r"[ \t]{0,4},\s{0,8}" + Q
         + _any("arguments", "parameters", "input") + Q + r"[ \t]{0,4}:")
    .add("", (r"<\|",), _any("tool", "tool_call", "tool_calls", "python_tag", "function", "plugin") + r"\|>")
)

# link_directive ───────────────────────────────────────────────────────────────
# Only with a sign of exfiltration: a send / upload / submit-to verb, a query string that carries data (a
# parameter named for data, or a template placeholder as its value), or an image that loads by itself.
# "Visit https://ncert.nic.in" and "Open https://www.youtube.com/watch?v=..." are ordinary teaching text.
_URL = _any("https?://", r"www\.")
_EXFIL_NAMES = _any(
    "data", "secret", "token", "key", "apikey", "api_key", "password", "passwd", "pwd", "prompt", "chat",
    "conversation", "history", "answers?", "messages?", "msg", "content", "payload", "info", "leak", "exfil",
    "session", "cookie", "text", "input", "output", "context", "memory", "q_?data",
)
_EXFIL_QUERY = r"[^\s\"'<>]{0,120}?[?&]" + _any(r"[a-z0-9_\-]{1,24}=[{\[<$%]", _EXFIL_NAMES + "=")
_HI_SEND = _any("भेजो", "भेजें", "भेजिए", "भेजना", "अपलोड" + S + _any("करो", "करें", "कीजिए"))
_HI_OPEN = _any("खोलो", "खोलें", "खोलिए", "खोलना", "पर" + S + _any("जाओ", "जाएं", "जाएँ", "जाइए"),
                "क्लिक" + S + _any("करो", "करें", "कीजिए"))
_MR_SEND = _any("पाठवा", "पाठव", "अपलोड" + S + _any("करा", "कर"))
_MR_OPEN = _any("उघडा", "उघड", "वर" + S + _any("जा", "जावा"), "क्लिक" + S + "करा")
LINK_DIRECTIVE = (
    _Rule()
    .add(B, ("visit", "click", "open", "go" + S + "to", "navigate" + S + "to", "browse" + S + "to", "load", "fetch",
             "follow", "access", "request", "curl", "wget"),
         E + r"[^\n]{0,40}?" + _URL + _EXFIL_QUERY)
    .add(B, ("send", "upload", "forward", "transmit", "leak", "exfiltrate"), E + r"[^\n]{0,40}?" + _URL)
    .add(B, ("submit",), E + r"[^\n]{0,40}?" + B + "to" + S + _URL)
    .add("", (r"!\[",), r"[^\]\n!]{0,100}\]\([ \t]{0,4}(?:https?:)?//")  # a markdown image: it loads by itself
    .add("", ("<img",), r"(?![a-z0-9])[^>\n]{0,200}?" + B + r"src[ \t]{0,3}=[ \t]{0,3}[\"']?[ \t]{0,3}(?:https?:)?//")
    .add("", ("https?://", r"www\."), r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any(_HI_SEND, _MR_SEND))
    .add("", ("https?://", r"www\."), _EXFIL_QUERY + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any(_HI_OPEN, _MR_OPEN))
)

# encoded_blob ─────────────────────────────────────────────────────────────────
# 200 base64 (or base64url) characters in a row with at least one digit, + or / AND at least one letter (a DNA
# sequence has no digit, the digits of pi have no letter). It can only start at the start of a run, so its
# lookaheads run once per run.
_B64 = "a-z0-9+/_\\-"
ENCODED_BLOB = f"(?<![{_B64}])(?=[{_B64}]{{0,199}}[0-9+/])(?=[{_B64}]{{0,199}}[a-z])[{_B64}]{{200}}"

_COMPILED = {
    code: rule.compile()
    for code, rule in (
        ("instruction_override", INSTRUCTION_OVERRIDE),
        ("role_markup", ROLE_MARKUP),
        ("role_play", ROLE_PLAY),
        ("prompt_extraction", PROMPT_EXTRACTION),
        ("model_directive", MODEL_DIRECTIVE),
        ("tool_call", TOOL_CALL),
        ("link_directive", LINK_DIRECTIVE),
    )
}
PATTERNS: dict[str, re.Pattern[str]] = {code: _COMPILED[code][0] for code in RULE_CODES if code in _COMPILED}
PATTERNS["encoded_blob"] = re.compile(ENCODED_BLOB)
TRIGGERS: dict[str, re.Pattern[str]] = {code: pair[1] for code, pair in _COMPILED.items()}


# ── Matching ─────────────────────────────────────────────────────────────────


def _search(pattern: re.Pattern[str], norm: str, start: int, end: int) -> re.Match[str] | None:
    """Every unanchored search of the scanner runs here (a test records the calls)."""
    return pattern.search(norm, start, end)


def _match(pattern: re.Pattern[str], norm: str, start: int, end: int) -> re.Match[str] | None:
    """Every anchored match of the scanner runs here (a test records the calls)."""
    return pattern.match(norm, start, end)


def _first_match(code: str, norm: str) -> re.Match[str] | None:
    """The leftmost match of one rule in the normalised text (see the module docstring)."""
    n = len(norm)
    pattern = PATTERNS[code]
    trigger = TRIGGERS.get(code)
    if trigger is None:
        return _search(pattern, norm, 0, n)
    pos = 0
    while pos < n:
        hit = _search(trigger, norm, pos, n)
        if hit is None:
            return None
        start = hit.start()
        found = _match(pattern, norm, start, min(n, start + ANCHORED_SPAN))
        if found is not None:
            return found
        pos = start + 1
    return None


# ── Public API ───────────────────────────────────────────────────────────────


def scan_text(text: str | None) -> tuple[str, ...]:
    """The codes found in `text`, in RULE_CODES order, each once. Empty or None gives ()."""
    if not text:
        return ()
    norm = normalize_for_scan(text)
    return tuple(code for code in RULE_CODES if _first_match(code, norm) is not None)


def scan_metadata(value: str | None) -> tuple[str, ...]:
    """A document title or an original file name: cleaned with the ingest rule for chunk text (clean_text),
    then scanned (review of 5d-2, finding 1). Never changes the stored value."""
    if not value:
        return ()
    return scan_text(clean_text(value))


def scan_chunk(heading_path: str | None, content: str) -> tuple[str, ...]:
    """The codes of one chunk: its heading path and content joined by a newline (as they are embedded)."""
    return scan_text(f"{heading_path}\n{content}" if heading_path else content)


class _ChunkText(Protocol):
    @property
    def heading_path(self) -> str | None: ...

    @property
    def content(self) -> str: ...


def scan_chunks(chunks: Sequence[_ChunkText]) -> list[tuple[str, ...]]:
    """One tuple of codes per chunk, in order. Reads the chunks only; never changes them."""
    return [scan_chunk(c.heading_path, c.content) for c in chunks]


# ── Where a match is: spans of the original text (for a teacher's review) ─────


@dataclass(frozen=True)
class Match:
    """The first match of one code, as text[start:end] of the ORIGINAL text."""

    code: str
    start: int
    end: int


def _original_index(text: str, norm_index: int) -> int:
    """The smallest prefix length p of `text` whose scan copy is at least `norm_index` characters long. The copy
    of a prefix only grows as the prefix grows, so this is a binary search over prefixes (each one normalised),
    O(n log n) for a chunk-sized text."""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if len(normalize_for_scan(text[:mid])) >= norm_index:
            hi = mid
        else:
            lo = mid + 1
    return lo


def find_matches(text: str) -> list[Match]:
    """The first match of each code (RULE_CODES order) mapped back to a span of `text`: the span starts at the
    character that produced the match's first scan character and ends after the one that produced its last.
    For review of stored chunks, titles and file names (bounded sizes): a pointer for a human, not a decision."""
    if not text:
        return []
    norm = normalize_for_scan(text)
    out: list[Match] = []
    for code in RULE_CODES:
        m = _first_match(code, norm)
        if m is not None:
            start = _original_index(text, m.start() + 1) - 1
            end = max(start + 1, _original_index(text, m.end()))
            out.append(Match(code=code, start=start, end=end))
    return out


def first_match_offset(text: str) -> int | None:
    """The offset in `text` (the original, not the copy) where the earliest match of any rule starts, or None."""
    starts = [m.start for m in find_matches(text)]
    return min(starts) if starts else None


EXCERPT_CHARS = 300  # the longest review excerpt
EXCERPT_LEAD = 80  # characters shown before a match (fewer when the match itself is long)

Source = Literal["heading_path", "content"]


@dataclass(frozen=True)
class ReviewMatch:
    """Where one code matched in a chunk, and at most EXCERPT_CHARS characters around it. UNTRUSTED text."""

    code: str
    source: Source
    excerpt: str


def _window(text: str, start: int, end: int) -> str:
    lead = min(EXCERPT_LEAD, max(0, EXCERPT_CHARS - (end - start)))
    first = max(0, start - lead)
    return text[first : first + EXCERPT_CHARS]


def review_matches(heading_path: str | None, content: str) -> list[ReviewMatch]:
    """Per code found in the chunk (scanned as the index stage scans it: heading path, newline, content): the
    source of the match and an excerpt around it. A match that starts in the heading path has the source
    heading_path (its excerpt comes from the joined text when it runs on into the content)."""
    heading = heading_path or ""
    joined = f"{heading}\n{content}" if heading else content
    offset = len(heading) + 1 if heading else 0
    out: list[ReviewMatch] = []
    for m in find_matches(joined):
        if heading and m.start < len(heading):
            text = heading if m.end <= len(heading) else joined
            out.append(ReviewMatch(m.code, "heading_path", _window(text, m.start, m.end)))
        else:
            start, end = max(0, m.start - offset), max(0, m.end - offset)
            out.append(ReviewMatch(m.code, "content", _window(content, start, end)))
    return out


def review_excerpt(content: str) -> str:
    """At most EXCERPT_CHARS characters of `content` for a teacher's review, around the first match in the
    content (or its start when nothing in the content matches). The UNTRUSTED text itself, unchanged."""
    matches = find_matches(content)
    if not matches:
        return content[:EXCERPT_CHARS]
    first = min(matches, key=lambda m: m.start)
    return _window(content, first.start, first.end)
