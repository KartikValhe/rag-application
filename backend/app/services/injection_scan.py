"""
app/services/injection_scan.py
──────────────────────────────
The prompt-injection tripwire (step 5d-2; design A6; docs/phase5d-plan.md section 7). Pure: no database, no
network, no model. It FLAGS text and never changes or drops it.

    scan_text(text)                    codes found in one text (a document title, a file name, ...)
    scan_chunk(heading_path, content)  codes of one chunk: heading path + "\\n" + content
    scan_chunks(chunks)                one tuple of codes per chunk (the index stage runs this in a thread)
    first_match_offset(text)           where the first match starts in the ORIGINAL text (for review excerpts)

Matching runs on a separate copy of the text: invisible format characters (Unicode category Cf, for example
zero-width and bidi controls) removed, Unicode tag characters (U+E0020-E007E, "ASCII smuggling") turned back
into the ASCII they hide, then NFKC, casefold and NFKC again. Full-width letters, mathematical letters,
no-break spaces and upper case therefore make no difference. The text itself is never modified.

Codes (the rule table of plan section 7, in this order): instruction_override, role_markup, role_play,
prompt_extraction, model_directive, tool_call, link_directive, encoded_blob. Codes never contain a comma (the
index stage joins them with commas).

THIS IS A TRIPWIRE, NOT A GUARANTEE. Paraphrases, other languages, text in images, a phrase split across two
chunks and anything not foreseen here pass unflagged, while a teacher's or a computer-science text ABOUT
prompts is flagged. A flag is a reason for a human to look; "not flagged" never means "safe".

Linear time (the ReDoS rule of upload_security.py), shown by tests without a clock:
  - Every rule is a list of alternatives that each start with a literal "trigger" (ignore, <|, http, पिछल ...).
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
from typing import Protocol

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

# ── The normalised copy ──────────────────────────────────────────────────────

# Every format character (category Cf) of the Unicode version of Python 3.12, as ranges (a test checks it).
_FORMAT_RANGES: tuple[tuple[int, int], ...] = (
    (0x00AD, 0x00AD), (0x0600, 0x0605), (0x061C, 0x061C), (0x06DD, 0x06DD), (0x070F, 0x070F),
    (0x0890, 0x0891), (0x08E2, 0x08E2), (0x180E, 0x180E), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x2064), (0x2066, 0x206F), (0xFEFF, 0xFEFF), (0xFFF9, 0xFFFB), (0x110BD, 0x110BD),
    (0x110CD, 0x110CD), (0x13430, 0x1343F), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
)
_TAG_ASCII_FIRST, _TAG_ASCII_LAST = 0xE0020, 0xE007E  # tag characters that mirror printable ASCII


def _format_map() -> dict[int, str | None]:
    table: dict[int, str | None] = {}
    for first, last in _FORMAT_RANGES:
        for cp in range(first, last + 1):
            table[cp] = None
    for cp in range(_TAG_ASCII_FIRST, _TAG_ASCII_LAST + 1):
        table[cp] = chr(cp - 0xE0000)
    return table


_FORMAT_MAP = _format_map()


def normalize_for_scan(text: str) -> str:
    """The copy the rules run on (see the module docstring). Never stored, never returned to a client."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text.translate(_FORMAT_MAP)).casefold())


# ── Building blocks ──────────────────────────────────────────────────────────
# B / E: the edges of a Latin word. "_" and "-" count as separators, so a file name such as
# ignore_all_previous_instructions.pdf matches like the sentence does.
B = r"(?<![a-z0-9])"
E = r"(?![a-z0-9])"
S = r"[\s_\-]{1,4}"  # one to four separators between two words
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


# instruction_override ─────────────────────────────────────────────────────────
_EN_OVERRIDE_VERBS = ("ignore", "disregard", "forget", "skip", "bypass")
_EN_EARLIER = _any("previous", "prior", "above", "earlier", "preceding", "former", "original", "initial", "system", "old")
_EN_ORDERS = _any(
    "instructions?", "prompts?", "rules?", "directions?", "directives?", "guidelines?", "commands?", "messages?",
    "context", "orders",
)
_HI_ORDERS = _any("निर्देश", "आदेश", "नियम", "हिदायत") + "(?:ों)?"
_HI_DISOBEY = _any(
    "अनदेखा", "अनदेखी", "नज" + NUKTA + "रअंदाज" + NUKTA, "भूल", "छोड" + NUKTA, "उपेक्षा", "मत" + S + "मानो",
    "न" + S + "मानें",
)
_MR_ORDERS = _any("सूचना", "सुचना", "निर्देश", "आदेश", "नियम") + "(?:ांकडे|ांना|ां|ाकडे|ना)?"
_MR_DISOBEY = "(?:कडे" + S + ")?" + _any("दुर्लक्ष", "विसर", "डावल", "पाळू" + S + "नका", "मानू" + S + "नका")

INSTRUCTION_OVERRIDE = (
    _Rule()
    .add(B, _EN_OVERRIDE_VERBS, S + "(?:" + _any("all", "any", "the", "your", "of", "these", "those") + S + "){0,3}"
         + _EN_EARLIER + S + _EN_ORDERS + E)
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
ROLE_MARKUP = (
    _Rule()
    .add("", (r"<\|",), _any(
        "im_start", "im_end", "im_sep", "system", "user", "assistant", "endoftext", "eot_id", "start_header_id",
        "end_header_id", "begin_of_text") + r"\|>")
    .add("", (r"\[/?inst\]", r"<</?sys>>"))
    # "### system": a heading-like marker after whitespace or at the start
    .add(r"(?<![^\s])", ("##",), r"#{0,4}[ \t]{0,4}" + _any("system", "instructions?", "assistant", "user") + E)
    # "system:" or "assistant:" at the very start of a line
    .add(r"(?<![^\n])", ("system", "assistant"), r"[ \t]{0,4}:")
)

# role_play ────────────────────────────────────────────────────────────────────
ROLE_PLAY = (
    _Rule()
    .add(B, ("you",), S + "are" + S + "now" + S + _any(
        "an?", "the", "my", "your", "no" + S + "longer", "free", "unrestricted", "unfiltered", "dan", "jailbroken",
        "in" + S + _any("developer", "god", "dan", "unrestricted") + S + "mode") + E)
    .add(B, ("from",), S + "now" + S + "on,?" + S + "you" + S + _any("are", "will", "must", "shall", "should", "can") + E)
    .add(B, ("act",), S + "as" + S + "(?:an?" + S + ")?" + _any(
        "ai", "assistant", "chatbot", "dan", "unrestricted", "unfiltered", "jailbroken", "evil", "language" + S + "model",
        "llm") + E)
    .add(B, ("pretend",), S + _any("to" + S + "be", "you" + S + "are", "that" + S + "you" + S + "are") + E)
    .add(B, ("developer",), S + "mode" + E)
    .add(B, ("jailbr",), _any("eak(?:s|ing|ed)?", "oken") + E)
    .add(B, ("do",), S + "anything" + S + "now" + E)
    .add(DB, ("अब",), S + "से" + S + _any("तुम", "तू"))
    .add(DB, ("आतापासून",), S + _any("तू", "तुम्ही"))
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
_EN_ORDER = _any(
    "you" + S + _any("must", "should", "shall", "will", "are" + S + "required" + S + "to", "have" + S + "to"),
    "please", "always", "never", "do" + S + "not", "don['’]t", "ignore", "remember", "from" + S + "now",
)
MODEL_DIRECTIVE = (
    _Rule()
    .add(B, ("ai", r"a\.i\.", "assistant", "chatbot", "chat" + S + "bot", "bot", "language" + S + "model", "llm",
             "chatgpt", "chat" + S + "gpt", r"gpt(?:-?[0-9]{1,2}(?:\.[0-9])?)?", "claude", "gemini", "copilot", "bard",
             "llama"),
         r"(?:" + S + "assistant)?[ \t]{0,2}[,:][ \t]{0,4}" + _EN_ORDER + E)
    .add(B, ("if",), S + _any(
        _any("a", "an", "the", "any") + S + _any("student", "user", "learner", "child", "pupil", "person"),
        _any("someone", "anyone", "anybody")) + S + "asks" + E + r"[^\n]{0,80}?" + B
        + _any("say", "answer", "respond", "reply", "tell" + S + _any("them", "him", "her")) + E)
    .add(DB, ("एआई", r"ए\.आई\.", "चैटबॉट", "चैटबोट", "बॉट", "भाषा" + S + "मॉडल"),
         r"[ \t]{0,2}[,:][ \t]{0,4}" + _any("तुम्हें", "तुम", "आपको", "आप", "हमेशा", "कभी", "कृपया"))
    .add(DB, ("अगर", "यदि", "जब"), S + "(?:कोई" + S + ")?"
         + _any("छात्र", "छात्रा", "विद्यार्थी", "उपयोगकर्ता", "यूज" + NUKTA + "र", "बच्चा")
         + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any("पूछे", "पूछता", "पूछती", "पूछें", "पूछेगा", "पूछ" + S + "ले")
         + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT
         + _any("कहना", "कहो", "कहें", "जवाब" + S + _any("दो", "देना"), "बताना", "बोलना", "उत्तर" + S + "दो"))
    .add(DB, ("एआय", "एआई", "चॅटबॉट", "चॅटबोट", "बॉट"),
         r"[ \t]{0,2}[,:][ \t]{0,4}" + _any("तू", "तुम्ही", "तुला", "तुम्हाला", "नेहमी", "कधीही", "कृपया"))
    .add(DB, ("जर", "जेव्हा"), S + "(?:" + _any("एखाद्या", "कोणी", "कोणत्याही") + S + ")?"
         + _any("विद्यार्थ्याने", "विद्यार्थिनीने", "विद्यार्थी", "वापरकर्त्याने", "वापरकर्ता", "मुलाने")
         + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any("विचारले", "विचारल्यास", "विचारेल", "विचारलं", "विचारला")
         + r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any("सांग", "उत्तर" + S + "दे", "म्हण"))
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
_URL = _any("https?://", r"www\.")
LINK_DIRECTIVE = (
    _Rule()
    .add(B, ("visit", "click", "open", "send", "fetch", "go" + S + "to", "navigate" + S + "to", "browse" + S + "to",
             "load", "post", "upload", "forward", "submit", "download", "curl", "wget", "email", "mail"),
         E + r"[^\n]{0,40}?" + _URL)
    .add("", (r"!\[",), r"[^\]\n!]{0,100}\]\([ \t]{0,4}(?:https?:)?//")  # a markdown image: fetching it can leak data
    .add("", ("https?://", r"www\."), r"[^\n]{0,60}?" + _DEVANAGARI_NEXT + _any(
        # Hindi
        "खोलो", "खोलें", "खोलिए", "खोलना", "भेजो", "भेजें", "भेजिए", "भेजना", "पर" + S + _any("जाओ", "जाएं", "जाएँ", "जाइए"),
        "क्लिक" + S + _any("करो", "करें", "कीजिए"),
        # Marathi
        "उघडा", "उघड", "पाठवा", "पाठव", "वर" + S + _any("जा", "जावा"), "क्लिक" + S + "करा"))
)

# encoded_blob ─────────────────────────────────────────────────────────────────
# 200 base64 (or base64url) characters in a row with at least one digit, + or / (a DNA sequence or a long
# word has none). It can only start at the start of a run, so its lookahead runs once per run.
_B64 = "a-z0-9+/_\\-"
ENCODED_BLOB = f"(?<![{_B64}])(?=[{_B64}]{{0,199}}[0-9+/])[{_B64}]{{200}}"

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


def first_match_offset(text: str) -> int | None:
    """The offset in `text` (the original, not the copy) where the earliest match of any rule starts, or None.
    Used to centre a review excerpt. The copy is built character by character here, so in rare cases where
    NFKC composes across characters it can differ from scan_text: a pointer for a human, not a decision."""
    parts: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        folded = normalize_for_scan(ch)
        parts.append(folded)
        origin.extend([i] * len(folded))
    norm = "".join(parts)
    starts = [m.start() for code in RULE_CODES if (m := _first_match(code, norm)) is not None]
    return origin[min(starts)] if starts else None


EXCERPT_CHARS = 300  # the longest review excerpt
EXCERPT_LEAD = 80  # characters shown before the first match


def review_excerpt(content: str) -> str:
    """At most EXCERPT_CHARS characters of `content` for a teacher's review, starting EXCERPT_LEAD characters
    before the first match (or at the start when the match is in the heading or nothing matches here).
    The excerpt is the UNTRUSTED text itself, unchanged."""
    offset = first_match_offset(content)
    start = 0 if offset is None else max(0, offset - EXCERPT_LEAD)
    return content[start : start + EXCERPT_CHARS]
