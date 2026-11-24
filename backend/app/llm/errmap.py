"""
app/llm/errmap.py
─────────────────
How an HTTP status, an error code inside a stream and a Retry-After header become the error types of
app/llm/errors.py (docs/phase6-plan.md 7.4). Pure functions: no network, no logging, and the body of a
provider's answer never goes in. Only the provider name and the model key reach an error.

Status table (the "chain" column is what plan 7.4 does next):

  400 413 422 other 4xx   LLMBadRequest      no fallback (our request is wrong)
  401                     LLMAuthError       no fallback, opens the breaker
  403                     LLMAuthError       (OpenRouter: LLMContentBlocked, its moderation / guardrail block)
  402 404 424 498 499     LLMUnavailable     fallback (no credit; model gone or no ZDR-eligible route; flex full)
  408                     LLMTimeout         fallback
  429                     LLMRateLimited     fallback, with Retry-After in seconds when the header is a number
  5xx                     LLMUnavailable     fallback
  1xx 2xx(not 200) 3xx    LLMUnavailable     a redirect is never followed, a success with no stream is no answer
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from app.llm.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMContentBlocked,
    LLMError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)

# A Retry-After above this is cut to it: a hostile or broken provider must not park a model for a day.
MAX_RETRY_AFTER_S: Final = 3600.0

# Error names that providers use instead of (or next to) a number. Lower case, '-' and ' ' read as '_'.
_NAME_TO_STATUS: Final = {
    # Google RPC names and the snake_case codes of the Gemini docs
    "invalid_argument": 400, "invalid_request": 400, "failed_precondition": 400, "invalid_request_error": 400,
    "unauthenticated": 401, "authentication": 401, "authentication_error": 401,
    "payment_required": 402,
    "permission_denied": 403,
    "not_found": 404, "model_not_found": 404,
    "resource_exhausted": 429, "rate_limit_exceeded": 429, "quota_exceeded": 429, "too_many_requests": 429,
    "rate_limit_error": 429,
    "internal": 500, "api_error": 500,
    "unavailable": 503, "service_unavailable": 503,
    "deadline_exceeded": 504,
}


@dataclass(frozen=True, slots=True)
class ErrorContext:
    provider: str
    model_key: str


def unavailable(ctx: ErrorContext) -> LLMUnavailable:
    return LLMUnavailable(provider=ctx.provider, model_key=ctx.model_key)


def bad_request(ctx: ErrorContext) -> LLMBadRequest:
    return LLMBadRequest(provider=ctx.provider, model_key=ctx.model_key)


def blocked(ctx: ErrorContext) -> LLMContentBlocked:
    return LLMContentBlocked(provider=ctx.provider, model_key=ctx.model_key)


def parse_retry_after(value: str | None) -> float | None:
    """Seconds as a plain number ('3', '1.5'); anything else (a date, text, a negative, NaN, infinity) is None."""
    if value is None:
        return None
    text = value.strip()
    if not text or not all(c in "0123456789." for c in text) or text.count(".") > 1:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if not math.isfinite(seconds):
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


def error_for_status(ctx: ErrorContext, status: int, headers: Mapping[str, str] | None = None) -> LLMError:
    where = {"provider": ctx.provider, "model_key": ctx.model_key}
    if status == 429:
        return LLMRateLimited(retry_after_s=parse_retry_after((headers or {}).get("retry-after")), **where)
    if status == 401:
        return LLMAuthError(**where)
    if status == 403:
        # OpenRouter answers 403 for a moderation flag or a guardrail block (docs, errors page); Groq and Gemini
        # answer 403 for a key that may not do this.
        return LLMContentBlocked(**where) if ctx.provider == "openrouter" else LLMAuthError(**where)
    if status == 408:
        return LLMTimeout(**where)
    if 400 <= status < 500 and status not in (402, 404, 424, 498, 499):
        return LLMBadRequest(**where)
    return LLMUnavailable(**where)


def status_from_error_code(code: object, name: object = None) -> int | None:
    """The HTTP-like status behind an error object found inside a stream: a number from 100 to 599, or a
    known error name. None when there is nothing usable (the caller treats that as 'unavailable')."""
    if isinstance(code, int) and not isinstance(code, bool):
        return code if 100 <= code <= 599 else None
    for candidate in (code, name):
        if isinstance(candidate, str):
            found = _NAME_TO_STATUS.get(candidate.strip().lower().replace("-", "_").replace(" ", "_"))
            if found is not None:
                return found
    return None


def error_for_stream_error(ctx: ErrorContext, code: object, name: object = None) -> LLMError:
    status = status_from_error_code(code, name)
    return unavailable(ctx) if status is None else error_for_status(ctx, status)
