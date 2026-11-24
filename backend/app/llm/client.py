"""
app/llm/client.py
─────────────────
The one interface every provider adapter and the fake implement (docs/phase6-plan.md 7.1).

`stream` is declared as a plain `def` returning an AsyncIterator, so an `async def` generator (which has no
`await` in front of the call) satisfies it. It yields TokenEvents, then at most one UsageEvent, then one
DoneEvent, and raises one of the errors in app/llm/errors.py instead of yielding anything else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from app.llm.types import LLMEvent, LLMRequest


@runtime_checkable
class LLMClient(Protocol):
    provider: str

    def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]: ...
