"""
Helpers for the chain and breaker tests (step 6a-4): a fake clock and a fixture that empties the llm:* keys.

The fake clock is the only clock the chain and the breaker see in these tests. Its `sleep` moves time on and
returns at once, so a retry delay, a Retry-After or a cooldown never makes a test wait.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.core import redis_client

# Kept at import: a test that makes Redis "go down" patches redis_client.get_redis, and the cleanup below must
# still reach the real one (the autouse network guard's monkeypatch outlives this fixture).
_real_get_redis = redis_client.get_redis

START_S = 1_800_000_000.0  # an ordinary wall-clock value, so millisecond scores look like real ones


class FakeClock:
    def __init__(self, start: float = START_S) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def now_ms(self) -> int:
        return int(round(self.now * 1000))

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def llm_keys() -> list[str]:
    r = await _real_get_redis()
    return sorted([key async for key in r.scan_iter(match="llm:*", count=500)])


async def _delete_llm_keys() -> None:
    r = await _real_get_redis()
    keys = [key async for key in r.scan_iter(match="llm:*", count=500)]
    if keys:
        await r.delete(*keys)


@pytest.fixture
async def clean_llm_keys() -> AsyncIterator[None]:
    """The breaker keys are per provider (llm:cb:groq), so every test starts and ends with none of them."""
    await _delete_llm_keys()
    yield
    await _delete_llm_keys()
