"""
app/llm/openai_compat.py
────────────────────────
What Groq and OpenRouter share: both speak the OpenAI chat-completions format. This file builds the message
list and reads ONE streamed chunk (docs/phase6-plan.md 7.3). No network code here.

A chunk is one JSON object. What is read, and nothing else:
  choices[0].delta.content      the text (reasoning fields, such as `reasoning`, are never read)
  choices[0].finish_reason      stop, length; content_filter is a block; tool_calls / function_call, and any
                                tool call in the delta, are LLMBadRequest because the model has no tools
  x_groq.usage, usage           prompt_tokens and completion_tokens, when both are believable integers
  error                         a failure inside the stream (OpenRouter: HTTP 200, then an error object)
Anything else in the object is ignored. Text that is not a JSON object is a malformed stream.

Checked against eight real Groq streams (docs/phase6-plan.md, "6a-6 real runs" record): the usage sits in the one
finish chunk, twice (x_groq.usage and a top-level usage), and the stream ends with `data: [DONE]`. Reasoning
arrives first as `delta.reasoning`, which is never read.
"""

from __future__ import annotations

from typing import Any

from app.llm.errmap import ErrorContext, bad_request, blocked, error_for_stream_error, unavailable
from app.llm.transport import Chunk, load_json, reported_count
from app.llm.types import FinishReason, LLMRequest

DONE_SENTINEL = "[DONE]"


def build_messages(req: LLMRequest) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": req.system}]
    messages.extend({"role": m.role, "content": m.content} for m in req.messages)
    return messages


def _usage(value: dict[str, Any]) -> tuple[int, int] | None:
    sources = []
    x_groq = value.get("x_groq")
    if isinstance(x_groq, dict):
        sources.append(x_groq.get("usage"))
    sources.append(value.get("usage"))
    for source in sources:
        if isinstance(source, dict):
            prompt = reported_count(source.get("prompt_tokens"))
            completion = reported_count(source.get("completion_tokens"))
            if prompt is not None and completion is not None:
                return prompt, completion
    return None


def _finish(ctx: ErrorContext, reason: object) -> FinishReason | None:
    if reason is None:
        return None
    if reason == "stop":
        return "stop"
    if reason == "length":
        return "length"
    if reason == "content_filter":
        raise blocked(ctx)
    if reason in ("tool_calls", "function_call"):
        raise bad_request(ctx)  # the model was given no tools (plan 5.6)
    raise unavailable(ctx)  # "error", or a reason this adapter does not know


def parse_chunk(ctx: ErrorContext, data: str) -> Chunk:
    value = load_json(data)
    if not isinstance(value, dict):
        raise unavailable(ctx)
    if "error" in value:
        error = value["error"]
        if not isinstance(error, dict):
            raise unavailable(ctx)
        name = error.get("status") if isinstance(error.get("status"), str) else error.get("type")
        raise error_for_stream_error(ctx, error.get("code"), name)
    usage = _usage(value)
    choices = value.get("choices")
    if choices is None or choices == []:
        return Chunk(usage=usage)
    if not isinstance(choices, list) or not isinstance(choices[0], dict):
        raise unavailable(ctx)
    choice = choices[0]
    text = ""
    delta = choice.get("delta")
    if delta is not None:
        if not isinstance(delta, dict):
            raise unavailable(ctx)
        if delta.get("tool_calls") or delta.get("function_call"):
            raise bad_request(ctx)
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise unavailable(ctx)
            text = content
    return Chunk(text=text, finish=_finish(ctx, choice.get("finish_reason")), usage=usage)
