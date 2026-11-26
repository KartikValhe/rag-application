"""
app/services/guardrails.py
──────────────────────────
The Phase 7 checkpoints (docs/phase6-plan.md 10.3; step 6a-10a). Pure: no database, no network.

Two checkpoints: `input` (after the router, before condensing) and `output` (after the stream, before persistence).
Phase 6 ships two real checks and nothing else:
- input_length (input): the question is at most `max_input_chars` characters (the route already refuses longer ones
  with 422 message_too_long; this is the same rule at the checkpoint Phase 7 extends).
- no_context (output): an answer must have had at least one source; a model text with no retrieved context is
  never shown (the turn calls no model then, so this is a second line).
Phase 7 adds its checks through `extra_input` / `extra_output` (moderation, PII detection, groundedness). They run
in order and the first refusal wins. GuardContext carries the hits with their injection flags for those checks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final


@dataclass(frozen=True, slots=True)
class GuardContext:
    question: str
    language: str
    hits: Sequence[Any] = ()  # the retrieval hits (with injection_flags) of this turn, empty before retrieval


@dataclass(frozen=True, slots=True)
class GuardResult:
    allowed: bool
    check: str | None = None  # the check that refused
    code: str | None = None  # a stable code for the stored record (never text)


PASS: Final = GuardResult(allowed=True)
DEFAULT_MAX_INPUT_CHARS: Final = 500  # CHAT_MAX_INPUT_CHARS

Check = Callable[[GuardContext, str | None], GuardResult]


def _input_length(limit: int) -> Check:
    def check(ctx: GuardContext, text: str | None) -> GuardResult:
        if len(ctx.question) > limit:
            return GuardResult(allowed=False, check="input_length", code="message_too_long")
        return PASS

    return check


def _no_context(ctx: GuardContext, text: str | None) -> GuardResult:
    if not ctx.hits:
        return GuardResult(allowed=False, check="no_context", code="no_context")
    return PASS


class GuardrailPipeline:
    def __init__(
        self,
        *,
        max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
        extra_input: Sequence[Check] = (),
        extra_output: Sequence[Check] = (),
    ) -> None:
        self._input: tuple[Check, ...] = (_input_length(max_input_chars), *extra_input)
        self._output: tuple[Check, ...] = (_no_context, *extra_output)

    @staticmethod
    def _run(checks: Sequence[Check], ctx: GuardContext, text: str | None) -> GuardResult:
        for check in checks:
            result = check(ctx, text)
            if not result.allowed:
                return result
        return PASS

    def check_input(self, ctx: GuardContext) -> GuardResult:
        return self._run(self._input, ctx, None)

    def check_output(self, ctx: GuardContext, text: str) -> GuardResult:
        return self._run(self._output, ctx, text)
