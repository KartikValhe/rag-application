"""
app/llm/gemini.py
─────────────────
The Gemini adapter, DEVELOPMENT ONLY with sample data (docs/phase6-plan.md 7.2): the free tier trains on
inputs, so the start-up policy refuses a Gemini model outside development.

POST /v1beta/models/{model}:streamGenerateContent?alt=sse. The key is sent in the `x-goog-api-key` header,
never as `?key=` (plan 7.6). The body holds the system instruction, the conversation (assistant turns are
called "model" by Google) and generationConfig with maxOutputTokens and temperature; nothing else, so no
tools, no safetySettings and no response schema.

What is read from a chunk: candidates[0].content.parts[].text (a part marked `thought` is never passed on),
candidates[0].finishReason, promptFeedback.blockReason (the prompt was blocked), usageMetadata, and an
`error` object. A functionCall part is LLMBadRequest because the model has no tools.

UNVERIFIED (docs/phase6-plan.md, 6a-3 build record): Google's reference lists these field names but shows no
example stream, so the exact chunk layout, the end of the stream (the connection just closes here) and the
header form for this method are not confirmed by a recording. Google's pages also say `temperature` is ignored
by Gemini 3.6 and newer and will be refused with a 400 in a later release.
Source: https://aistudio.google.com/api-reference/generate-content/stream-generate-content (checked 2026-11-24).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Final

from app.llm.errmap import ErrorContext, bad_request, blocked, error_for_stream_error, unavailable
from app.llm.transport import Chunk, HTTPAdapter, load_json, reported_count
from app.llm.types import FinishReason, LLMRequest

BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")  # no '/', ':', '@' or '..' can reach the URL path
_BLOCKING: Final = frozenset(
    {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"}
)
_TOOL_RELATED: Final = frozenset({"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS"})


def build_body(req: LLMRequest) -> dict[str, Any]:
    return {
        "systemInstruction": {"parts": [{"text": req.system}]},
        "contents": [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]} for m in req.messages
        ],
        "generationConfig": {"maxOutputTokens": req.max_output_tokens, "temperature": req.temperature},
    }


def _usage(value: dict[str, Any]) -> tuple[int, int] | None:
    meta = value.get("usageMetadata")
    if not isinstance(meta, dict):
        return None
    prompt = reported_count(meta.get("promptTokenCount"))
    answer = reported_count(meta.get("candidatesTokenCount"))
    if prompt is None or answer is None:
        return None
    thoughts = reported_count(meta.get("thoughtsTokenCount")) or 0  # thinking tokens are billed as output
    return prompt, answer + thoughts


def _finish(ctx: ErrorContext, reason: object) -> FinishReason | None:
    if reason is None or reason in ("", "FINISH_REASON_UNSPECIFIED"):
        return None
    if reason == "STOP":
        return "stop"
    if reason == "MAX_TOKENS":
        return "length"
    if isinstance(reason, str) and reason in _BLOCKING:
        raise blocked(ctx)
    if isinstance(reason, str) and reason in _TOOL_RELATED:
        raise bad_request(ctx)
    raise unavailable(ctx)  # OTHER, LANGUAGE, MALFORMED_RESPONSE, a value this adapter does not know


def parse_chunk(ctx: ErrorContext, data: str) -> Chunk:
    value = load_json(data)
    if not isinstance(value, dict):
        raise unavailable(ctx)
    if "error" in value:
        error = value["error"]
        if not isinstance(error, dict):
            raise unavailable(ctx)
        name = error.get("status") if isinstance(error.get("status"), str) else None
        raise error_for_stream_error(ctx, error.get("code"), name)
    feedback = value.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason") not in (None, "", "BLOCK_REASON_UNSPECIFIED"):
        raise blocked(ctx)  # the prompt itself was refused: no candidates follow
    usage = _usage(value)
    candidates = value.get("candidates")
    if candidates is None or candidates == []:
        return Chunk(usage=usage)
    if not isinstance(candidates, list) or not isinstance(candidates[0], dict):
        raise unavailable(ctx)
    candidate = candidates[0]
    text = ""
    content = candidate.get("content")
    if content is not None:
        parts = content.get("parts") if isinstance(content, dict) else None
        if parts is not None and not isinstance(parts, list):
            raise unavailable(ctx)
        for part in parts or []:
            if not isinstance(part, dict):
                raise unavailable(ctx)
            if part.get("functionCall"):
                raise bad_request(ctx)
            piece = part.get("text")
            if piece is not None and not isinstance(piece, str):
                raise unavailable(ctx)
            if piece and part.get("thought") is not True:
                text += piece
    return Chunk(text=text, finish=_finish(ctx, candidate.get("finishReason")), usage=usage)


class GeminiClient(HTTPAdapter):
    provider = "gemini"
    KEY_SETTING: ClassVar[str] = "GEMINI_API_KEY"
    DONE_SENTINEL: ClassVar[str | None] = None  # the stream just closes
    __slots__ = ()

    def _url(self, model_id: str, ctx: ErrorContext) -> str:
        if not _MODEL_ID.match(model_id) or ".." in model_id:
            raise bad_request(ctx)
        return f"{BASE}{model_id}:streamGenerateContent?alt=sse"

    def _headers(self, key: str) -> dict[str, str]:
        return {"x-goog-api-key": key, "Content-Type": "application/json", "Accept": "text/event-stream"}

    def _body(self, req: LLMRequest, model_id: str) -> dict[str, Any]:
        return build_body(req)

    def _parse(self, ctx: ErrorContext, data: str) -> Chunk:
        return parse_chunk(ctx, data)
