"""
app/llm/openrouter.py
─────────────────────
The OpenRouter adapter (docs/phase6-plan.md 7.2, 7.3): POST /api/v1/chat/completions with stream=true.

EVERY request carries `provider: {"data_collection": "deny", "zdr": true}`: only zero-data-retention routes
that do not store data are eligible. There is no way to switch this off from outside this file; if no route
qualifies OpenRouter answers 404 or 503 and the chain moves to the next model (LLMUnavailable), it never
relaxes the restriction. No optional headers (HTTP-Referer, X-Title) are sent: they would tell OpenRouter which
site this is and the plan does not list them. Usage arrives by itself in the last chunk (no stream_options).
Source: https://openrouter.ai/docs/api-reference/streaming and
https://openrouter.ai/docs/features/provider-routing (checked 2026-11-24).
"""

from __future__ import annotations

from typing import Any, ClassVar, Final

from app.llm.errmap import ErrorContext
from app.llm.openai_compat import DONE_SENTINEL, build_messages, parse_chunk
from app.llm.transport import Chunk, HTTPAdapter
from app.llm.types import LLMRequest

URL = "https://openrouter.ai/api/v1/chat/completions"
ZDR_ONLY: Final = (("data_collection", "deny"), ("zdr", True))


def build_body(req: LLMRequest, model_id: str) -> dict[str, Any]:
    return {
        "model": model_id,
        "messages": build_messages(req),
        "max_completion_tokens": req.max_output_tokens,
        "temperature": req.temperature,
        "stream": True,
        "provider": dict(ZDR_ONLY),
    }


class OpenRouterClient(HTTPAdapter):
    provider = "openrouter"
    KEY_SETTING: ClassVar[str] = "OPENROUTER_API_KEY"
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
