"""
app/services/chat_stream_filter.py
──────────────────────────────────
The server-side filter between the LLM chain and the SSE writer (docs/phase6-plan.md 4.5, 5.6, 6.1, 6.3; step 6a-10a).
Pure: no database, no network, no clock. One StreamFilter per answer; feed() gets each model text piece, finish()
gets the provider's finish reason. What feed() and finish() return is exactly what the client is sent, and
`released_text` is exactly what is stored, so stored text and streamed text are the same by construction.

What it enforces (the guarantees do not depend on the model obeying the prompt):
1. Verdict gate (4.5, owner decision of the 6a-9a brief). Model text arrives in sub-word pieces (ANSWER + ABLE), so
   NOTHING is released until the first line is complete (its newline, or the end of the stream). Leading whitespace
   (spaces, tabs, blank lines) does not count and is not streamed. The line is accepted after dropping trailing spaces
   and tabs, markdown emphasis (**, __ or `, the same on both sides) and one trailing colon (inside or outside the
   emphasis), when what is left is exactly ANSWERABLE or NOT_IN_SOURCES: case-sensitive, no other normalisation.
   - NOT_IN_SOURCES: no model text is ever released; feed() asks the caller to close the provider stream at once.
   - ANSWERABLE: the line is dropped (and blank lines after it), the rest streams.
   - Anything else ("neither", R17), or a first line over 40 characters: the text streams as written, and the
     citation rule below decides at the end.
2. Characters (6.1, owner decision 2): U+2010 and U+2011 become "-" on every piece. No other character is changed.
3. Labels (6.3). A label group is `[S1]`, `[S1, S3]` or the full-width `【S1】` / `【S1, S3】` (one label is S plus ASCII
   digits). Text from a "[" or "【" is held back while it can still become a label group (at most MAX_LABEL_HOLD_CHARS),
   so a label split over pieces is handled. A complete group is validated against this turn's label table: labels
   in the table pass (a full-width group is rewritten to ASCII brackets BEFORE validation), labels not in it are
   removed before the client sees them and counted; a group with no valid label disappears. Anything that cannot be
   a label group is released unchanged. A label-shaped group longer than MAX_LABEL_HOLD_CHARS is dropped (its labels
   counted as invalid) together with the rest of it (label characters up to its closing bracket), so a very long
   group cannot carry an unchecked label to the client. An unfinished group at
   the end of the stream (no closing bracket) is released as plain text: it is not a label.
4. Caps (5.6): at most `max_chars` characters (8,000) are released; past it the answer is cut (outcome truncated).
   A run of LEAK_WINDOW_CHARS (60) characters that also appears in the system prompt (compared with whitespace runs
   collapsed and case folded) cuts the answer (outcome blocked_leak, retracted).
5. "No valid citation, no answer" (4.5 rule 2): an answer that ends with no valid label is retracted.

finish() gives the outcome: refused_by_model, blocked_leak, no_valid_citations, truncated (cut by the cap, or the
provider's finish reason "length") or answered. `retract` tells the caller to replace whatever was shown with the
fixed text (the SSE `replace` event); the filter itself never chooses a text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal, get_args

VERDICT_ANSWERABLE: Final = "ANSWERABLE"
VERDICT_NOT_IN_SOURCES: Final = "NOT_IN_SOURCES"
VERDICTS: Final = (VERDICT_ANSWERABLE, VERDICT_NOT_IN_SOURCES)

MAX_VERDICT_LINE_CHARS: Final = 40  # plan 4.5: the first line, not counting leading whitespace
MAX_ANSWER_CHARS: Final = 8000  # plan 5.6: the server cap on the released text
LEAK_WINDOW_CHARS: Final = 60  # plan 5.6: a verbatim run of the system prompt this long cuts the answer
MAX_LABEL_HOLD_CHARS: Final = 64  # the longest text held back while it can still become a label group
MAX_LABEL_DIGITS: Final = 4  # S1 .. S9999: more digits cannot be a label of any turn

LEADING_WHITESPACE: Final = " \t\r\n"
TRAILING_WHITESPACE: Final = " \t"  # owner decision: only spaces and tabs at the end of the first line
EMPHASIS: Final = ("**", "__", "`")
CHAR_MAP: Final = {ord("\u2010"): "-", ord("\u2011"): "-"}
OPENERS: Final = {"[": "]", "【": "】"}
LABEL_BODY_CHARS: Final = frozenset("S0123456789, ")
LABEL_RE: Final = re.compile(rf"S[1-9][0-9]{{0,{MAX_LABEL_DIGITS - 1}}}")

Outcome = Literal["answered", "refused_by_model", "no_valid_citations", "truncated", "blocked_leak"]
StopReason = Literal["refused_by_model", "truncated", "blocked_leak"]
FinishReason = Literal["stop", "length"]
VerdictForm = Literal["exact", "tolerant", "neither"]


@dataclass(frozen=True, slots=True)
class FilterStep:
    text: str  # send this to the client now (may be empty)
    stop: StopReason | None  # set: close the provider stream; nothing more will be released


@dataclass(frozen=True, slots=True)
class FilterResult:
    text: str  # the last text to send now (what was held back at the end)
    outcome: Outcome
    retract: bool  # True: replace everything shown with the fixed text
    verdict: str | None
    verdict_form: VerdictForm
    cited_labels: tuple[str, ...]  # valid labels, in the order of first citation
    invalid_citation_count: int
    answer_text: str  # everything released: the text to store when not retracted


def normalise_chunk(text: str) -> str:
    """U+2010 and U+2011 to ASCII '-'; nothing else (owner decision 2)."""
    return text.translate(CHAR_MAP)


def _core(line: str) -> str:
    """The line without its emphasis (the same marker on both sides) and one trailing colon."""
    for marker in EMPHASIS:
        if line.startswith(marker):
            rest = line[len(marker):]
            if rest.endswith(marker + ":"):  # **WORD**:
                return rest[: -len(marker) - 1]
            if len(rest) > len(marker) and rest.endswith(marker):  # **WORD** or **WORD:**
                rest = rest[: -len(marker)]
                return rest[:-1] if rest.endswith(":") else rest
            return ""  # opened and not closed: not a verdict
    return line[:-1] if line.endswith(":") else line


def parse_first_line(line: str) -> tuple[str | None, VerdictForm]:
    """The verdict of a complete first line (plan 4.5 and the owner's trailing-whitespace decision)."""
    stripped = line.lstrip(LEADING_WHITESPACE)
    if len(stripped) > MAX_VERDICT_LINE_CHARS:
        return None, "neither"
    core = _core(stripped.rstrip(TRAILING_WHITESPACE))
    if core not in VERDICTS:
        return None, "neither"
    return core, ("exact" if line == core else "tolerant")


def _label_state(held: str) -> Literal["open", "complete", "invalid"]:
    """Whether `held` (starting with an opener) is a prefix of a label group, a complete one, or neither."""
    # What comes next: "S" the letter S; "D1" the first digit; "D" a digit, a comma, a space or the closer;
    # "SP" a space or a comma; "C" (after a comma) a space or the next S.
    closer = OPENERS[held[0]]
    state = "S"
    digits = 0
    for ch in held[1:]:
        if state == "S" or (state == "C" and ch == "S"):
            if ch != "S":
                return "invalid"
            state = "D1"
        elif state == "C":
            if ch != " ":
                return "invalid"
        elif state == "D1":
            # S0 and S007 are label-shaped: they are validated (and removed), not shown as plain text
            if not "0" <= ch <= "9":
                return "invalid"
            state, digits = "D", 1
        elif state == "D":
            if "0" <= ch <= "9":
                digits += 1
                if digits > MAX_LABEL_DIGITS:
                    return "invalid"
            elif ch == ",":
                state = "C"
            elif ch == " ":
                state = "SP"
            elif ch == closer:
                return "complete"
            else:
                return "invalid"
        elif state == "SP":
            if ch == ",":
                state = "C"
            elif ch != " ":
                return "invalid"
    return "open"


class _LeakWatch:
    """Every LEAK_WINDOW_CHARS-long run of the system prompt (whitespace runs collapsed, case folded), and a rolling
    tail of the released text in the same form."""

    def __init__(self, system_prompt: str) -> None:
        norm = self._norm_all(system_prompt)
        self._windows = {norm[i:i + LEAK_WINDOW_CHARS] for i in range(len(norm) - LEAK_WINDOW_CHARS + 1)}
        self._tail = ""
        self._last_space = True

    @staticmethod
    def _norm_all(text: str) -> str:
        return re.sub(r"\s+", " ", text.casefold()).strip()

    def hit(self, text: str) -> bool:
        for ch in text:
            if ch.isspace():
                if self._last_space:
                    continue
                self._last_space = True
                part = " "
            else:
                self._last_space = False
                part = ch.casefold()
            for c in part:
                self._tail = (self._tail + c)[-LEAK_WINDOW_CHARS:]
                if len(self._tail) == LEAK_WINDOW_CHARS and self._tail in self._windows:
                    return True
        return False


class StreamFilter:
    def __init__(
        self,
        labels: Iterable[str],
        *,
        system_prompt: str,
        max_chars: int = MAX_ANSWER_CHARS,
        require_citation: bool = True,
    ) -> None:
        table = tuple(labels)
        for label in table:
            if not isinstance(label, str) or LABEL_RE.fullmatch(label) is None:
                raise ValueError("a label table holds labels S1, S2, ... only")
        if max_chars < 1:
            raise ValueError("max_chars must be at least 1")
        self._labels = frozenset(table)
        self._max_chars = max_chars
        self._require_citation = require_citation
        self._leak = _LeakWatch(system_prompt)
        self._phase: Literal["verdict", "body"] = "verdict"
        self._line = ""  # the first line so far, without its leading whitespace
        self._had_leading = False  # leading whitespace was dropped (the form is then "tolerant", not "exact")
        self._strip_body_start = False  # after an ANSWERABLE line: blank lines before the answer are not streamed
        self._hold = ""
        self._skip_closer: str | None = None  # dropping the rest of an over-long label-shaped group
        self._skipped = ""
        self._released: list[str] = []
        self._released_len = 0
        self._stop: StopReason | None = None
        self._verdict: str | None = None
        self._form: VerdictForm = "neither"
        self._cited: list[str] = []
        self._invalid = 0
        self._finished = False

    @property
    def released_text(self) -> str:
        return "".join(self._released)

    @property
    def stopped(self) -> StopReason | None:
        return self._stop

    # ── output ──

    def _emit(self, text: str, out: list[str]) -> None:
        """Release text to the client, under the cap and the leak watch."""
        if not text or self._stop is not None:
            return
        room = self._max_chars - self._released_len
        cut = len(text) > room
        if cut:
            text = text[:room]
        if self._leak.hit(text):
            self._stop = "blocked_leak"
            return
        if text:
            out.append(text)
            self._released.append(text)
            self._released_len += len(text)
        if cut:
            self._stop = "truncated"

    def _emit_group(self, held: str, out: list[str]) -> None:
        """A complete label group: only its labels in the table are released (a full-width group in ASCII brackets).
        A group is released whole or not at all, and its labels count only once it was released."""
        found = re.findall(r"S[0-9]+", held[1:-1])
        valid: list[str] = []
        invalid = 0
        for label in found:
            if label in self._labels:
                if label not in valid:
                    valid.append(label)
            else:
                invalid += 1
        if not valid:
            text = ""
        elif held[0] == "[" and len(valid) == len(found):
            text = held  # unchanged, keeps the model's own spacing
        else:
            text = "[" + ", ".join(valid) + "]"
        if text and len(text) > self._max_chars - self._released_len:
            self._stop = "truncated"  # no half label at the cap
            return
        before = self._released_len
        self._emit(text, out)
        if self._released_len - before != len(text):
            return  # stopped by the leak watch: nothing of it was released
        self._invalid += invalid
        for label in valid:
            if label not in self._cited:
                self._cited.append(label)

    def _body(self, text: str, out: list[str]) -> None:
        if self._strip_body_start:
            text = text.lstrip(LEADING_WHITESPACE)
            if not text:
                return
            self._strip_body_start = False
        pending = list(reversed(text))
        plain: list[str] = []
        while pending and self._stop is None:
            ch = pending.pop()
            if self._skip_closer is not None:
                # the rest of a dropped over-long group: dropped too, up to its closer or the first other character
                if ch == self._skip_closer or ch in LABEL_BODY_CHARS:
                    self._skipped += ch
                    if ch == self._skip_closer:
                        self._end_skip()
                    continue
                self._end_skip()
            if not self._hold:
                if ch in OPENERS:
                    self._emit("".join(plain), out)
                    plain = []
                    self._hold = ch
                else:
                    plain.append(ch)
                continue
            self._hold += ch
            state = _label_state(self._hold)
            if state == "complete":
                self._emit("".join(plain), out)
                plain = []
                self._emit_group(self._hold, out)
                self._hold = ""
            elif state == "invalid":
                # not a label: the opener is plain text; the rest is read again (it may hold a new opener)
                plain.append(self._hold[0])
                pending.extend(reversed(self._hold[1:]))
                self._hold = ""
            elif len(self._hold) > MAX_LABEL_HOLD_CHARS:
                # still label-shaped but longer than any honest group: dropped with whatever of it still follows,
                # so no unchecked label is shown
                self._skip_closer, self._skipped = OPENERS[self._hold[0]], self._hold
                self._hold = ""
        self._emit("".join(plain), out)

    def _end_skip(self) -> None:
        self._invalid += len(re.findall(r"S[0-9]+", self._skipped))
        self._skip_closer, self._skipped = None, ""

    # ── the verdict line ──

    def _decide(self, line: str, rest: str, out: list[str], *, at_end: bool) -> None:
        verdict, form = parse_first_line(line)
        if form == "exact" and self._had_leading:
            form = "tolerant"
        self._verdict, self._form, self._phase = verdict, form, "body"
        if verdict == VERDICT_NOT_IN_SOURCES:
            self._stop = "refused_by_model"
            return
        if verdict == VERDICT_ANSWERABLE:
            self._strip_body_start = True
            self._body(rest, out)
            return
        self._body(line + ("" if at_end else "\n") + rest, out)

    def feed(self, chunk: str) -> FilterStep:
        out: list[str] = []
        if self._finished:
            raise RuntimeError("the filter has finished")
        if self._stop is None:
            text = normalise_chunk(chunk)
            if self._phase == "verdict":
                if not self._line:
                    stripped = text.lstrip(LEADING_WHITESPACE)
                    self._had_leading = self._had_leading or len(stripped) != len(text)
                    text = stripped
                self._line += text
                if "\n" in self._line:
                    line, rest = self._line.split("\n", 1)
                    self._line = ""
                    self._decide(line, rest, out, at_end=False)
                elif len(self._line) > MAX_VERDICT_LINE_CHARS:
                    held, self._line = self._line, ""
                    self._phase = "body"
                    self._body(held, out)
            else:
                self._body(text, out)
        return FilterStep("".join(out), self._stop)

    def finish(self, finish_reason: FinishReason = "stop") -> FilterResult:
        if finish_reason not in get_args(FinishReason):
            raise ValueError("finish_reason must be 'stop' or 'length'")
        if self._finished:
            raise RuntimeError("the filter has finished")
        self._finished = True
        out: list[str] = []
        if self._stop is None and self._phase == "verdict":
            line, self._line = self._line, ""
            self._decide(line, "", out, at_end=True)
        if self._skip_closer is not None:
            self._end_skip()
        if self._stop is None and self._hold:
            held, self._hold = self._hold, ""
            self._emit(held, out)  # an unfinished label group is plain text
        outcome: Outcome
        if self._stop == "refused_by_model":
            outcome = "refused_by_model"
        elif self._stop == "blocked_leak":
            outcome = "blocked_leak"
        elif self._require_citation and not self._cited:
            outcome = "no_valid_citations"
        elif self._stop == "truncated" or finish_reason == "length":
            outcome = "truncated"
        else:
            outcome = "answered"
        return FilterResult(
            text="".join(out),
            outcome=outcome,
            retract=outcome in ("refused_by_model", "blocked_leak", "no_valid_citations"),
            verdict=self._verdict,
            verdict_form=self._form,
            cited_labels=tuple(self._cited),
            invalid_citation_count=self._invalid,
            answer_text=self.released_text,
        )
