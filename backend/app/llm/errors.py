"""
app/llm/errors.py
─────────────────
The error types of the LLM client (docs/phase6-plan.md 7.1, 7.4, 7.6).

A runtime error (LLMError) carries NO free text: only the provider name and the model key, both checked
against a strict pattern. A provider's response body, a request detail or a key therefore cannot end up in an
error message, in a log line or in a response. Adapters raise these types and nothing else.

LLMPolicyError and ModelRegistryError are start-up problems, not LLMError: the chain catches LLMError to fall
back to the next model, and a configuration mistake must never be swallowed there.
"""

from __future__ import annotations

import math
import re
from typing import ClassVar

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_MODEL_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}\Z")


class LLMError(Exception):
    """Base of every runtime LLM error."""

    # Whether the chain may try the next model after this error (plan 7.4, only before the first token).
    falls_back: ClassVar[bool] = False
    summary: ClassVar[str] = "LLM error"

    def __init__(self, *, provider: str | None = None, model_key: str | None = None) -> None:
        if provider is not None and not _NAME.match(provider):
            raise ValueError("provider must be a short lower-case name")
        if model_key is not None and not _MODEL_KEY.match(model_key):
            raise ValueError("model_key has characters a model key never has")
        self.provider = provider
        self.model_key = model_key
        super().__init__(self._text())

    def _text(self) -> str:
        parts = [self.summary]
        if self.provider:
            parts.append(f"provider={self.provider}")
        if self.model_key:
            parts.append(f"model={self.model_key}")
        return " ".join(parts)


class LLMRateLimited(LLMError):
    """HTTP 429 or a quota message. The model is skipped until `retry_after_s` has passed."""

    falls_back = True
    summary = "LLM rate limited"

    def __init__(
        self, *, retry_after_s: float | None = None, provider: str | None = None, model_key: str | None = None
    ) -> None:
        if retry_after_s is not None and (
            isinstance(retry_after_s, bool) or not math.isfinite(retry_after_s) or retry_after_s < 0
        ):
            raise ValueError("retry_after_s must be a finite number of seconds, zero or more")
        self.retry_after_s = retry_after_s
        super().__init__(provider=provider, model_key=model_key)


class LLMUnavailable(LLMError):
    """The provider is down, overloaded or returned a server error, or no ZDR-eligible route is left."""

    falls_back = True
    summary = "LLM unavailable"


class LLMTimeout(LLMError):
    """A connect, first-token, idle or total timeout."""

    falls_back = True
    summary = "LLM timeout"


class LLMBadRequest(LLMError):
    """The provider refused the request, or answered with something the plan never allows (a tool call)."""

    summary = "LLM bad request"


class LLMContentBlocked(LLMError):
    """The provider's safety filter blocked the prompt or the answer."""

    summary = "LLM content blocked"


class LLMAuthError(LLMError):
    """The key was refused. Opens that provider's breaker and goes to the operator log; never falls back."""

    summary = "LLM authentication failed"


class LLMPolicyError(Exception):
    """The configuration breaks a rule of the plan: the app refuses to start. Lists every problem found."""

    def __init__(self, problems: tuple[str, ...] | list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


class ModelRegistryError(LLMPolicyError):
    """app/llm/models.json is missing, not valid JSON, or breaks its schema."""
