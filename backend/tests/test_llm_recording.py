"""
Step 6a-6 (docs/phase6-plan.md 6a-6, 7.6): the dev-only recording hook of app/llm/transport.py.

The hook writes the RAW lines of a provider's SSE body to one file in tests/fixtures/llm/, and nothing else: no
header, no request body, no URL, no key. It is off by default and refuses to turn on outside development.

Everything runs on httpx.MockTransport and a canary key. These tests never write into the real fixtures folder:
`transport.RECORD_DIR` is pointed at a temporary folder (one test checks what the real value is).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.llm import transport
from app.llm.errors import LLMPolicyError
from app.llm.groq import GroqClient
from app.llm.types import DoneEvent, LLMMessage, LLMRequest, LLMTimeouts, TokenEvent

CANARY = "gsk_canary_4c1d9e7a22b3f5a8_must_never_be_recorded"
REQUEST_TEXT = "REQUEST-TEXT-CANARY what is a prop in React"
SYSTEM_TEXT = "SYSTEM-TEXT-CANARY answer only from the sources"
MODEL = "groq:openai/gpt-oss-120b"


def req() -> LLMRequest:
    return LLMRequest(
        model_key=MODEL,
        system=SYSTEM_TEXT,
        messages=(LLMMessage(role="user", content=REQUEST_TEXT),),
        max_output_tokens=300,
        timeouts=LLMTimeouts(connect=5.0, first_token=10.0, idle=15.0, total=45.0),
        request_id="req-recording-0001",
    )


def chunk(text: str = "", finish: str | None = None, **extra: Any) -> bytes:
    choice: dict[str, Any] = {"index": 0, "delta": {"content": text} if text else {}, "finish_reason": finish}
    return b"data: " + json.dumps({"choices": [choice], **extra}).encode() + b"\n\n"


GOOD_BODY = chunk("ANSWERABLE\nA prop is an input.") + chunk("", "stop") + b"data: [DONE]\n\n"


class Pieces(httpx.AsyncByteStream):
    """A body that arrives in the given pieces (so a test can cut a line, or the key, in two)."""

    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = pieces

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in self.pieces:
            yield piece


class Wire:
    def __init__(self, body: bytes | list[bytes], *, status: int = 200, on_request: Callable[[], None] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._on_request = on_request
        pieces = [body] if isinstance(body, bytes) else body
        self._pieces = pieces
        self._status = status
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._on_request is not None:
            self._on_request()
        return httpx.Response(
            self._status,
            headers={"content-type": "text/event-stream", "x-response-header-canary": "RESPONSE-HEADER-CANARY"},
            stream=Pieces(self._pieces),
        )


async def run(wire: Wire) -> list[object]:
    client = GroqClient(api_key=SecretStr(CANARY), http=wire.http)
    return [event async for event in client.stream(req())]


@pytest.fixture
def record_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "fixtures_llm"
    folder.mkdir()
    monkeypatch.setattr(transport, "RECORD_DIR", folder)
    monkeypatch.delenv(transport.RECORD_ENV, raising=False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    return folder


def turn_on(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv(transport.RECORD_ENV, str(path))


# ── off by default ───────────────────────────────────────────────────────────────────────────────────


async def test_the_hook_is_off_by_default(record_dir: Path) -> None:
    events = await run(Wire(GOOD_BODY))
    assert isinstance(events[-1], DoneEvent)
    assert list(record_dir.iterdir()) == []


async def test_an_empty_setting_is_off(record_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(transport.RECORD_ENV, "")
    await run(Wire(GOOD_BODY))
    assert list(record_dir.iterdir()) == []


def test_the_real_record_folder_is_the_llm_fixture_folder() -> None:
    expected = Path(__file__).resolve().parent / "fixtures" / "llm"
    assert Path(transport.RECORD_DIR).resolve() == expected


def test_the_setting_name_is_not_a_pydantic_setting() -> None:
    # a hook that lived in the settings file could be switched on by a deployment's .env; the environment
    # variable is read by transport.py alone, and only while ENVIRONMENT=development
    assert not hasattr(settings, transport.RECORD_ENV)


# ── what the file holds ──────────────────────────────────────────────────────────────────────────────


async def test_the_raw_body_lines_are_written_and_the_answer_is_unchanged(
    record_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = record_dir / "recorded_one.sse"
    turn_on(monkeypatch, target)
    events = await run(Wire(GOOD_BODY))
    assert "".join(e.text for e in events if isinstance(e, TokenEvent)) == "ANSWERABLE\nA prop is an input."
    assert isinstance(events[-1], DoneEvent) and events[-1].finish_reason == "stop"
    assert target.read_bytes() == GOOD_BODY


async def test_a_line_cut_in_two_pieces_is_written_whole(record_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = record_dir / "recorded_split.sse"
    turn_on(monkeypatch, target)
    cut = len(GOOD_BODY) // 3
    await run(Wire([GOOD_BODY[:cut], GOOD_BODY[cut : cut + 7], GOOD_BODY[cut + 7 :]]))
    assert target.read_bytes() == GOOD_BODY


async def test_nothing_but_the_body_is_recorded(record_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = record_dir / "recorded_clean.sse"
    turn_on(monkeypatch, target)
    wire = Wire(GOOD_BODY)
    await run(wire)
    written = target.read_text(encoding="utf-8")
    lowered = written.lower()
    sent = wire.requests[0]
    assert CANARY not in written
    assert REQUEST_TEXT not in written and SYSTEM_TEXT not in written
    assert "RESPONSE-HEADER-CANARY" not in written
    header_names = {name.lower() for name in sent.headers} | {"authorization", "x-response-header-canary", "content-type"}
    for name in header_names:
        assert name not in lowered, name
    assert "api.groq.com" not in lowered and "chat/completions" not in lowered  # no URL
    assert str(sent.url) not in written


async def test_a_key_the_provider_echoes_is_not_recorded_even_when_cut_in_two(
    record_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = record_dir / "recorded_echo.sse"
    turn_on(monkeypatch, target)
    echoed = chunk(f"your key is {CANARY} ok") + chunk("", "stop") + b"data: [DONE]\n\n"
    where = echoed.index(CANARY.encode()) + 9
    await run(Wire([echoed[:where], echoed[where:]]))
    written = target.read_text(encoding="utf-8")
    assert CANARY not in written
    assert "[redacted]" in written  # the key was replaced, the rest of the line is still raw


async def test_the_key_is_in_no_recorded_byte_or_error_text(record_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = record_dir / "recorded_key.sse"
    turn_on(monkeypatch, target)
    await run(Wire(GOOD_BODY))
    assert CANARY.encode() not in target.read_bytes()


# ── only a complete, successful answer is recorded ───────────────────────────────────────────────────


async def test_a_failed_response_writes_no_file(record_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = record_dir / "recorded_fail.sse"
    turn_on(monkeypatch, target)
    with pytest.raises(Exception):  # noqa: B017, PT011  (an LLMError of the 429 kind)
        await run(Wire(b'{"error":"slow down"}', status=429))
    assert not target.exists()


async def test_a_stream_that_ends_before_a_finish_reason_writes_no_file(
    record_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = record_dir / "recorded_cut.sse"
    turn_on(monkeypatch, target)
    with pytest.raises(Exception):  # noqa: B017, PT011  (LLMUnavailable: truncated)
        await run(Wire(chunk("half an answer")))
    assert not target.exists()


async def test_an_existing_file_is_never_overwritten(record_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = record_dir / "recorded_exists.sse"
    target.write_bytes(b"keep me")
    turn_on(monkeypatch, target)
    wire = Wire(GOOD_BODY)
    with pytest.raises(LLMPolicyError):
        await run(wire)
    assert target.read_bytes() == b"keep me"
    assert wire.requests == []  # refused before any request left


async def test_a_file_that_appears_while_the_request_is_running_is_not_overwritten(
    record_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the second line of defence: the file is opened with "x", so a race with another writer cannot overwrite it
    target = record_dir / "recorded_race.sse"
    turn_on(monkeypatch, target)
    events = await run(Wire(GOOD_BODY, on_request=lambda: target.write_bytes(b"someone else wrote this")))
    assert isinstance(events[-1], DoneEvent)  # the answer itself is not harmed
    assert target.read_bytes() == b"someone else wrote this"


# ── refusals ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("environment", ["production", "staging", "test", "Production", ""])
async def test_it_refuses_to_turn_on_outside_development(
    environment: str, record_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)
    target = record_dir / "recorded_prod.sse"
    turn_on(monkeypatch, target)
    wire = Wire(GOOD_BODY)
    with pytest.raises(LLMPolicyError) as caught:
        await run(wire)
    assert not target.exists()
    assert wire.requests == []  # no request was sent, so nothing was spent either
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize(
    "where",
    [
        "outside/recorded.sse",  # another folder
        "../recorded.sse",  # parent of the record folder
        "sub/recorded.sse",  # a sub folder
        "recorded.txt",  # not an .sse file
        "recorded",  # no extension
        ".sse",  # no name
    ],
)
async def test_it_writes_only_a_sse_file_directly_inside_the_record_folder(
    where: str, record_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (record_dir / "sub").mkdir()
    (record_dir.parent / "outside").mkdir(exist_ok=True)
    turn_on(monkeypatch, record_dir / where)
    wire = Wire(GOOD_BODY)
    before = sorted(p for p in tmp_path.rglob("*"))
    with pytest.raises(LLMPolicyError):
        await run(wire)
    assert sorted(p for p in tmp_path.rglob("*")) == before
    assert wire.requests == []


async def test_a_relative_path_is_refused(record_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(transport.RECORD_ENV, "recorded_relative.sse")
    with pytest.raises(LLMPolicyError):
        await run(Wire(GOOD_BODY))


async def test_the_hook_changes_nothing_when_off_even_with_a_failing_stream(record_dir: Path) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011
        await run(Wire(b"", status=500))
    assert list(record_dir.iterdir()) == []
