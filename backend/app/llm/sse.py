"""
app/llm/sse.py
──────────────
A small Server-Sent Events line parser (docs/phase6-plan.md 7.2: "their own SSE line parser").

`SSEParser.feed(bytes)` returns the data of every event that finished in that piece of the stream. Rules
(the WHATWG event-stream format, cut down to what the providers send):
  * lines end with LF, CRLF or CR; a CRLF split between two pieces is still one line end;
  * a line starting with ':' is a comment (OpenRouter's ": OPENROUTER PROCESSING") and is ignored;
  * 'data: x' adds a data line (one leading space after the colon is removed); several data lines of one
    event join with a newline; every other field (event, id, retry) is ignored;
  * a blank line ends the event; an event with no data line is skipped;
  * an event that never got its blank line (the stream was cut) is NOT delivered;
  * the bytes must be UTF-8, and one line may not grow past MAX_LINE_BYTES (a stream with no line end
    must not fill memory): both raise SSEError, which carries no text of the stream.
"""

from __future__ import annotations

import codecs
from typing import Final

MAX_LINE_BYTES: Final = 262_144


class SSEError(ValueError):
    """The stream is not valid UTF-8, or a line is too long. Never carries any of the stream's text."""


class SSEParser:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._line = ""  # the line being read
        self._line_bytes = 0
        self._after_cr = False  # the last character was a CR: a following LF is part of the same line end
        self._data: list[str] | None = None  # data lines of the current event, None when it has none
        self._first = True  # nothing decoded yet (a byte-order mark is dropped here)

    def feed(self, chunk: bytes) -> list[str]:
        try:
            text = self._decoder.decode(chunk)
        except UnicodeDecodeError:
            raise SSEError("stream is not valid UTF-8") from None
        if self._first and text:
            text = text.removeprefix("﻿")
            self._first = False
        events: list[str] = []
        for char in text:
            if self._after_cr:
                self._after_cr = False
                if char == "\n":
                    continue
            if char in "\r\n":
                self._after_cr = char == "\r"
                self._end_of_line(events)
                continue
            self._line += char
            self._line_bytes += 1 if char < "\x80" else len(char.encode("utf-8"))
            if self._line_bytes > MAX_LINE_BYTES:
                raise SSEError("line too long")
        return events

    def _end_of_line(self, events: list[str]) -> None:
        line, self._line, self._line_bytes = self._line, "", 0
        if not line:  # blank line: the event is complete
            if self._data is not None:
                events.append("\n".join(self._data))
            self._data = None
            return
        if line.startswith(":"):
            return
        name, _, value = line.partition(":")
        if name != "data":
            return
        self._data = (self._data or []) + [value.removeprefix(" ")]
