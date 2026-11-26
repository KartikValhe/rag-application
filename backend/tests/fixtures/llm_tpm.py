"""
Helpers for the token-window tests (step 6a-10b): a Redis stand-in that records every command and can hold the
first token-window command of each task at a barrier, so a concurrency test FORCES the overlap instead of hoping
for it (the same idea as OverlapGate in test_llm_budget.py and ClaimEventLog in tests/security/worker_assertions.py).
"""

from __future__ import annotations

import asyncio
from typing import Any

TPM_PREFIX = "llm:tpm:"


def touches_tpm(args: tuple[Any, ...]) -> bool:
    """True when any positional argument of a Redis command is a token-window key (EVAL puts its keys there)."""
    return any(isinstance(a, str) and a.startswith(TPM_PREFIX) for a in args)


class SpyRedis:
    """
    Wraps the real Redis client. Records (command, args) for every call. With `parties`, the FIRST command of
    each task that touches a token-window key waits at a barrier until `parties` tasks have each sent theirs;
    only then do they all go on. A check-then-write implementation (two commands) is therefore certain to let
    every task check before any of them writes. One atomic script cannot be split this way.
    """

    def __init__(self, real: Any, parties: int | None = None) -> None:
        self._real = real
        self._barrier = asyncio.Barrier(parties) if parties else None
        self._seen: set[int] = set()
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.events: list[tuple[str, int]] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args))
            task = id(asyncio.current_task())
            if self._barrier is not None and touches_tpm(args) and task not in self._seen:
                self._seen.add(task)
                self.events.append(("enter", task))
                async with asyncio.timeout(10):
                    await self._barrier.wait()
            return await attr(*args, **kwargs)

        return call

    def tpm_calls(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [(name, args) for name, args in self.calls if touches_tpm(args)]

    def done(self) -> None:
        self.events.append(("done", id(asyncio.current_task())))


def assert_all_overlapped(events: list[tuple[str, int]], parties: int) -> None:
    """Every task was in flight (had sent its first window command) before the first one finished."""
    enters = [i for i, (kind, _) in enumerate(events) if kind == "enter"]
    dones = [i for i, (kind, _) in enumerate(events) if kind == "done"]
    assert len(enters) == parties, f"expected {parties} tasks in flight, saw {len(enters)}: {events}"
    assert len({t for kind, t in events if kind == "enter"}) == parties, f"not {parties} separate tasks: {events}"
    assert len(dones) == parties, f"not every task finished: {events}"
    assert min(dones) > max(enters), f"a task finished before the last one started, no overlap: {events}"
