"""
app/llm/types.py
────────────────
The request and the events of the LLM client (docs/phase6-plan.md 7.1, 5.6, 7.4).

The model gets NO tools. LLMRequest and LLMMessage have no field for tools, functions, tool_choice or
response_format; they are frozen slotted dataclasses, so a request cannot be built with one (TypeError) and
cannot grow one later. A message role is "user" or "assistant": the system prompt is its own field.

The text of the question, the system prompt and the answer is left out of every repr, so logging an object
by accident does not write what a student asked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, get_args

if TYPE_CHECKING:
    from app.core.config import Settings

Role = Literal["user", "assistant"]
FinishReason = Literal["stop", "length"]

DEFAULT_TEMPERATURE: Final = 0.2
MAX_TEMPERATURE: Final = 2.0


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_seconds(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMTimeouts:
    """The four limits of plan 7.4, in seconds. Required on every request: there is no hidden default."""

    connect: float
    first_token: float
    idle: float
    total: float

    def __post_init__(self) -> None:
        for name in ("connect", "first_token", "idle", "total"):
            if not _is_seconds(getattr(self, name)):
                raise ValueError(f"timeout {name} must be a finite number of seconds above 0")
        if self.first_token > self.total or self.idle > self.total:
            raise ValueError("first_token and idle must not exceed total: they could never fire")

    @classmethod
    def from_settings(cls, s: Settings) -> LLMTimeouts:
        return cls(
            connect=s.LLM_TIMEOUT_CONNECT,
            first_token=s.LLM_TIMEOUT_FIRST_TOKEN,
            idle=s.LLM_TIMEOUT_IDLE,
            total=s.LLM_TIMEOUT_TOTAL,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMMessage:
    role: Role
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.role not in get_args(Role):
            raise ValueError("message role must be 'user' or 'assistant'")
        if not isinstance(self.content, str):
            raise TypeError("message content must be text")
        if not self.content:
            raise ValueError("message content must not be empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMRequest:
    model_key: str
    system: str = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    max_output_tokens: int
    timeouts: LLMTimeouts
    request_id: str
    temperature: float = DEFAULT_TEMPERATURE

    def __post_init__(self) -> None:
        if not isinstance(self.model_key, str) or not self.model_key:
            raise ValueError("model_key must be a non-empty string")
        if not isinstance(self.system, str) or not self.system:
            raise ValueError("system must be a non-empty string")
        if not isinstance(self.messages, tuple):
            raise ValueError("messages must be a tuple, so the request stays immutable")
        if not self.messages or not all(isinstance(m, LLMMessage) for m in self.messages):
            raise ValueError("messages must be a non-empty tuple of LLMMessage")
        if not _is_count(self.max_output_tokens) or self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be an integer of at least 1")
        if not isinstance(self.timeouts, LLMTimeouts):
            raise TypeError("timeouts must be an LLMTimeouts")
        if not isinstance(self.request_id, str) or not self.request_id:
            raise ValueError("request_id must be a non-empty string")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not math.isfinite(self.temperature)
            or not 0.0 <= self.temperature <= MAX_TEMPERATURE
        ):
            raise ValueError(f"temperature must be between 0 and {MAX_TEMPERATURE}")


@dataclass(frozen=True, slots=True)
class TokenEvent:
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("token text must be a string")


@dataclass(frozen=True, slots=True)
class UsageEvent:
    prompt_tokens: int
    completion_tokens: int
    estimated: bool  # True when the provider reported nothing and we counted (plan 7.7)

    def __post_init__(self) -> None:
        if not _is_count(self.prompt_tokens) or not _is_count(self.completion_tokens):
            raise ValueError("token counts must be integers of zero or more")
        if not isinstance(self.estimated, bool):
            raise TypeError("estimated must be a bool")


@dataclass(frozen=True, slots=True)
class DoneEvent:
    finish_reason: FinishReason

    def __post_init__(self) -> None:
        if self.finish_reason not in get_args(FinishReason):
            raise ValueError("finish_reason must be 'stop' or 'length'")


LLMEvent = TokenEvent | UsageEvent | DoneEvent
