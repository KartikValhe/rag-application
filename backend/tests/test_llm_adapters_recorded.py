"""
Step 6a-6b (docs/phase6-plan.md, 6a-3 UNVERIFIED list): the Groq adapter against the REAL streams that the owner
recorded with `llm_smoke --record` on 2026-11-25/26 (tests/fixtures/llm/recorded_groq_*.sse).

Nothing here reaches a network: each recording is the body of an httpx.MockTransport response. The expected values
are read from the recording by this file's own small reader (plain json), not by the adapter, so a parser bug
cannot hide behind itself. The key is a canary.

What the real streams look like (found in 6a-6b): a role chunk with empty content, then `delta.reasoning` chunks
(with a `channel` field), then `delta.content` chunks, then ONE finish chunk with an empty delta that carries the
usage twice (`x_groq.usage` and a top-level `usage`), then `data: [DONE]`. No separate usage chunk.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.llm.errors import LLMError
from app.llm.groq import GroqClient
from app.llm.types import (
    DoneEvent,
    LLMEvent,
    LLMMessage,
    LLMRequest,
    LLMTimeouts,
    TokenEvent,
    UsageEvent,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm"
CANARY = "sk-canary-7f3a9c1e5b2d4a60-must-never-appear"

# model id, language, kind, provider-reported (prompt, completion) tokens of that recording
RECORDINGS = [
    ("openai/gpt-oss-120b", "en", "answerable", (1161, 101)),
    ("openai/gpt-oss-120b", "en", "near_topic", (1124, 71)),
    ("openai/gpt-oss-120b", "mr", "answerable", (1303, 132)),
    ("openai/gpt-oss-120b", "mr", "near_topic", (1338, 95)),
    ("openai/gpt-oss-20b", "en", "answerable", (1189, 86)),
    ("openai/gpt-oss-20b", "en", "near_topic", (1138, 72)),
    ("openai/gpt-oss-20b", "mr", "answerable", (1289, 245)),
    ("openai/gpt-oss-20b", "mr", "near_topic", (1296, 91)),
]
IDS = [f"{m.split('-')[-1]}-{lang}-{kind}" for m, lang, kind, _ in RECORDINGS]
SECRET_WORDS = ("gsk_", "authorization", "bearer", "api_key")


def path_of(model_id: str, lang: str, kind: str) -> Path:
    return FIXTURES / f"recorded_groq_{model_id.replace('/', '_')}_{lang}_{kind}.sse"


# ── the test's own reader of a recording (no adapter code) ──────────────────────────────────────────


@dataclass(frozen=True)
class Truth:
    content: list[str]  # every non-empty delta.content, in order
    reasoning: list[str]  # every non-empty delta.reasoning, in order
    usage: tuple[int, int]  # from the finish chunk
    finishes: list[str]
    done_is_last: bool


def read_truth(raw: bytes) -> Truth:
    content: list[str] = []
    reasoning: list[str] = []
    finishes: list[str] = []
    usage: tuple[int, int] | None = None
    payloads = [line[5:].strip() for line in raw.decode("utf-8").split("\n") if line.startswith("data:")]
    for payload in payloads:
        if payload == "[DONE]":
            continue
        value = json.loads(payload)
        for choice in value.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                content.append(delta["content"])
            if delta.get("reasoning"):
                reasoning.append(delta["reasoning"])
            if choice.get("finish_reason"):
                finishes.append(choice["finish_reason"])
        source = value.get("x_groq", {}).get("usage") or value.get("usage")
        if source:
            usage = (source["prompt_tokens"], source["completion_tokens"])
    assert usage is not None, "the recording carries no usage"
    return Truth(content, reasoning, usage, finishes, payloads[-1] == "[DONE]")


# ── running a recording through the adapter ─────────────────────────────────────────────────────────


def make_req(model_id: str) -> LLMRequest:
    return LLMRequest(
        model_key=f"groq:{model_id}",
        system="You answer only from the sources.",
        messages=(LLMMessage(role="user", content="What is a prop?"),),
        max_output_tokens=600,
        timeouts=LLMTimeouts(connect=5.0, first_token=10.0, idle=15.0, total=45.0),
        request_id="req-internal-0001",
    )


async def run(model_id: str, raw: bytes) -> tuple[list[LLMEvent], LLMError | None]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    events: list[LLMEvent] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GroqClient(api_key=SecretStr(CANARY), http=http)
        try:
            async for event in client.stream(make_req(model_id)):
                events.append(event)
        except LLMError as exc:
            return events, exc
    return events, None


def texts(events: Iterable[LLMEvent]) -> list[str]:
    return [e.text for e in events if isinstance(e, TokenEvent)]


# ── the recordings themselves ───────────────────────────────────────────────────────────────────────


def test_all_eight_recordings_are_there() -> None:
    present = sorted(p.name for p in FIXTURES.glob("recorded_groq_*.sse"))
    assert present == sorted(path_of(m, lang, kind).name for m, lang, kind, _ in RECORDINGS)


@pytest.mark.parametrize(("model_id", "lang", "kind", "counts"), RECORDINGS, ids=IDS)
def test_a_recording_holds_no_secret(model_id: str, lang: str, kind: str, counts: tuple[int, int]) -> None:
    text = path_of(model_id, lang, kind).read_text(encoding="utf-8").lower()
    for word in SECRET_WORDS:
        assert word not in text, f"{word!r} found in a recording"
    assert not re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", text), "something that looks like an e-mail address"


@pytest.mark.parametrize(("model_id", "lang", "kind", "counts"), RECORDINGS, ids=IDS)
def test_the_recording_has_the_real_shape_these_tests_rely_on(
    model_id: str, lang: str, kind: str, counts: tuple[int, int]
) -> None:
    truth = read_truth(path_of(model_id, lang, kind).read_bytes())
    assert truth.reasoning, "no reasoning chunk: the 'reasoning is never passed on' tests would prove nothing"
    assert truth.content
    assert truth.finishes == ["stop"]
    assert truth.done_is_last
    assert truth.usage == counts


# ── the adapter on each real stream ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("model_id", "lang", "kind", "counts"), RECORDINGS, ids=IDS)
async def test_the_joined_token_text_is_the_visible_answer(
    model_id: str, lang: str, kind: str, counts: tuple[int, int]
) -> None:
    raw = path_of(model_id, lang, kind).read_bytes()
    truth = read_truth(raw)
    events, error = await run(model_id, raw)
    assert error is None
    assert texts(events) == truth.content  # same pieces, same order, nothing added, nothing dropped
    assert "".join(texts(events)) == "".join(truth.content)


@pytest.mark.parametrize(("model_id", "lang", "kind", "counts"), RECORDINGS, ids=IDS)
async def test_reasoning_text_never_reaches_a_token_event(
    model_id: str, lang: str, kind: str, counts: tuple[int, int]
) -> None:
    raw = path_of(model_id, lang, kind).read_bytes()
    truth = read_truth(raw)
    events, error = await run(model_id, raw)
    assert error is None
    answer = "".join(texts(events))
    thinking = "".join(truth.reasoning)
    assert len(thinking) >= 20
    assert thinking not in answer
    assert all(isinstance(e, (TokenEvent, UsageEvent, DoneEvent)) for e in events)


@pytest.mark.parametrize(("model_id", "lang", "kind", "counts"), RECORDINGS, ids=IDS)
async def test_the_provider_reported_usage_is_used_not_an_estimate(
    model_id: str, lang: str, kind: str, counts: tuple[int, int]
) -> None:
    events, error = await run(model_id, path_of(model_id, lang, kind).read_bytes())
    assert error is None
    usage = [e for e in events if isinstance(e, UsageEvent)]
    assert len(usage) == 1
    assert (usage[0].prompt_tokens, usage[0].completion_tokens) == counts
    assert usage[0].estimated is False


@pytest.mark.parametrize(("model_id", "lang", "kind", "counts"), RECORDINGS, ids=IDS)
async def test_the_stream_ends_with_usage_then_done_with_finish_reason_stop(
    model_id: str, lang: str, kind: str, counts: tuple[int, int]
) -> None:
    events, error = await run(model_id, path_of(model_id, lang, kind).read_bytes())
    assert error is None
    assert isinstance(events[-1], DoneEvent) and events[-1].finish_reason == "stop"
    assert isinstance(events[-2], UsageEvent)
    assert all(isinstance(e, TokenEvent) for e in events[:-2])
    assert sum(isinstance(e, DoneEvent) for e in events) == 1


@pytest.mark.parametrize(("model_id", "lang", "kind", "counts"), RECORDINGS, ids=IDS)
async def test_the_answer_starts_with_the_verdict_word(
    model_id: str, lang: str, kind: str, counts: tuple[int, int]
) -> None:
    events, error = await run(model_id, path_of(model_id, lang, kind).read_bytes())
    assert error is None
    pieces = texts(events)
    verdict = "NOT_IN_SOURCES" if kind == "near_topic" else "ANSWERABLE"
    assert "".join(pieces).startswith(verdict)
    # The verdict does not arrive as one piece: the real tokens are ANSWER+ABLE and NOT+_IN+_S+OURCES. What the
    # stream filter will see first is a PREFIX of the word, so it must work on prefixes (plan 4.5).
    assert pieces[0] and verdict.startswith(pieces[0])
    if kind == "near_topic":
        assert "".join(pieces) == verdict  # a refusal is the verdict word and nothing else


@pytest.mark.parametrize("keep", ["x_groq_only", "top_level_only"])
async def test_usage_is_read_from_either_place_it_is_sent(keep: str) -> None:
    """The real finish chunk carries the usage twice. Each copy alone must still be read."""
    model_id, lang, kind, counts = RECORDINGS[0]
    lines = []
    for line in path_of(model_id, lang, kind).read_text(encoding="utf-8").split("\n"):
        if line.startswith("data: {") and '"usage"' in line:
            value: dict[str, Any] = json.loads(line[6:])
            if keep == "x_groq_only":
                del value["usage"]
            else:
                del value["x_groq"]["usage"]
            line = "data: " + json.dumps(value, ensure_ascii=False)
        lines.append(line)
    events, error = await run(model_id, "\n".join(lines).encode("utf-8"))
    assert error is None
    usage = [e for e in events if isinstance(e, UsageEvent)]
    assert [(u.prompt_tokens, u.completion_tokens, u.estimated) for u in usage] == [(*counts, False)]


# ── the full-width citation found in the smoke output (mr-03, 120b), not in any recording ───────────


async def test_a_full_width_citation_passes_through_the_adapter_unchanged() -> None:
    """SYNTHETIC stream, built from the real answer of 120b mr-03 in the smoke output (no recorded .sse holds it).

    The answer cited with U+3010 / U+3011 instead of ASCII brackets. The adapter must not touch the text; turning
    it into [S1] before citation validation is a job of step 6a-9a (see the 6a-6 real runs record).
    """
    pieces = ["ANSWERABLE", "\n", "बाल घटकाला ती वाचनीय (read‑only) स्वरूपात मिळते", "【", "S", "1", "】"]
    raw = b"".join(
        b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]}, ensure_ascii=False).encode() + b"\n\n"
        for p in pieces
    )
    raw += b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    events, error = await run("openai/gpt-oss-120b", raw)
    assert error is None
    assert texts(events) == pieces
    assert "".join(texts(events)).endswith("S1】")
    assert "[S1]" not in "".join(texts(events))
