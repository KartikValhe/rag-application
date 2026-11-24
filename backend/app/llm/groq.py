"""
app/llm/groq.py
───────────────
The Groq adapter (docs/phase6-plan.md 7.2, 7.3): POST /openai/v1/chat/completions with stream=true.

The body holds only what the plan lists: the model, the messages (system first), max_completion_tokens
(`max_tokens` is deprecated on Groq), temperature and stream. No tools, no response_format, no stream_options,
no user field, and nothing that names a user, a tenant or a conversation. The key goes into the
Authorization header and nowhere else.
Source of the endpoint and the field names: https://console.groq.com/docs/api-reference (checked 2026-11-24).
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.llm.errmap import ErrorContext
from app.llm.openai_compat import DONE_SENTINEL, build_messages, parse_chunk
from app.llm.transport import Chunk, HTTPAdapter
from app.llm.types import LLMRequest

URL = "https://api.groq.com/openai/v1/chat/completions"


def build_body(req: LLMRequest, model_id: str) -> dict[str, Any]:
    return {
        "model": model_id,
        "messages": build_messages(req),
        "max_completion_tokens": req.max_output_tokens,
        "temperature": req.temperature,
        "stream": True,
    }


class GroqClient(HTTPAdapter):
    provider = "groq"
    KEY_SETTING: ClassVar[str] = "GROQ_API_KEY"
    DONE_SENTINEL: ClassVar[str | None] = DONE_SENTINEL
    __slots__ = ()

    def _url(self, model_id: str, ctx: ErrorContext) -> str:
        return URL

    def _headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "text/event-stream"}

    def _body(self, req: LLMRequest, model_id: str) -> dict[str, Any]:
        return build_body(req, model_id)

    def _parse(self, ctx: ErrorContext, data: str) -> Chunk:
        return parse_chunk(ctx, data)
