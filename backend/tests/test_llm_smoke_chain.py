"""
Step 6a-6: llm_smoke through the REAL LLMChain (breaker, cooldowns, RPM/RPD slots on the test Redis), with
FakeLLM as the only provider. The stub-chain tests of test_llm_smoke.py cover the script's own rules; this file
proves the two things that need the real chain: the script drives it through its public entry point, and a 429
the chain gave up on (Retry-After above 5 s) is waited out by reading the cooldown the chain set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core import redis_client
from app.core.config import settings
from app.llm.breaker import Breaker, Cooldowns
from app.llm.chain import LLMChain
from app.llm.errors import LLMRateLimited
from app.llm.fake import FakeLLM, Script
from app.llm.types import UsageEvent
from app.scripts import llm_smoke as smoke
from tests.fixtures.llm_chain import FakeClock, clean_llm_keys  # noqa: F401  (pytest fixture)

pytestmark = pytest.mark.usefixtures("clean_llm_keys")

MODEL = "groq:openai/gpt-oss-120b"


def ok_script() -> Script:
    return Script(tokens=("ANSWERABLE\n", "A prop is an input [S1]."), usage=UsageEvent(900, 40, False))


@pytest.fixture(autouse=True)
def dev_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "LLM_ANSWER_CHAIN", "")
    monkeypatch.setattr(settings, "GROQ_API_KEY", SecretStr("gsk_canary_not_a_key"))


def real_chain(fake: FakeLLM, clock: FakeClock, *, fake_time: bool = False) -> LLMChain:
    # the registry is the shipped models.json. By default the breaker and cooldowns use the real wall clock, as the
    # script's own cooldown reader does; only the chain's retry sleep and deadline clock are fake, so nothing
    # really waits. With fake_time the cooldown also follows the fake clock, so a retry the chain waits out
    # itself finds the cooldown over.
    now_ms = clock.now_ms if fake_time else None
    return LLMChain(
        clients={"groq": fake},
        breaker=Breaker(now_ms=now_ms),
        cooldowns=Cooldowns(now_ms=now_ms),
        clock=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 1.0,
    )


def rows(out: Path) -> list[dict[str, object]]:
    path = out / f"{smoke.model_slug(MODEL)}_en.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def test_the_script_runs_through_the_real_chain(tmp_path: Path) -> None:
    fake = FakeLLM(default=ok_script())
    clock = FakeClock()
    parsed = smoke.parse_args(["--model", MODEL, "--lang", "en", "--out", str(tmp_path / "o"), "--pause", "0", "--count", "3"])
    code = await smoke.run_smoke(parsed, chain=real_chain(fake, clock), sleep=clock.sleep, clock=clock.monotonic)
    assert code == 0
    assert [r["outcome"] for r in rows(tmp_path / "o")] == ["ok"] * 3
    assert [c.model_key for c in fake.calls] == [MODEL] * 3
    assert settings.LLM_ANSWER_CHAIN == ""


async def test_a_429_the_chain_gave_up_on_is_waited_out_through_the_cooldown_it_set(tmp_path: Path) -> None:
    fake = FakeLLM((Script(error=LLMRateLimited(retry_after_s=20.0, provider="groq", model_key=MODEL)),), default=ok_script())
    clock = FakeClock()
    redis = await redis_client.get_redis()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await redis.delete(f"llm:cooldown:{MODEL}")  # the time the script waited has passed

    parsed = smoke.parse_args(["--model", MODEL, "--lang", "en", "--out", str(tmp_path / "o"), "--pause", "0", "--count", "1"])
    code = await smoke.run_smoke(parsed, chain=real_chain(fake, clock), sleep=sleep, clock=clock.monotonic)
    assert code == 0
    assert len(slept) == 1 and 19.0 <= slept[0] <= 22.0  # the cooldown the chain started, not the 30 s pause
    row = rows(tmp_path / "o")[0]
    assert row["outcome"] == "ok" and row["rate_limit_waits"] == 1
    assert fake.call_count == 2


async def test_a_short_retry_after_is_the_chains_own_business(tmp_path: Path) -> None:
    fake = FakeLLM((Script(error=LLMRateLimited(retry_after_s=2.0, provider="groq", model_key=MODEL)),), default=ok_script())
    clock = FakeClock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    parsed = smoke.parse_args(["--model", MODEL, "--lang", "en", "--out", str(tmp_path / "o"), "--pause", "0", "--count", "1"])
    await smoke.run_smoke(parsed, chain=real_chain(fake, clock, fake_time=True), sleep=sleep, clock=clock.monotonic)
    row = rows(tmp_path / "o")[0]
    assert row["outcome"] == "ok" and row["rate_limit_waits"] == 0 and slept == []
    assert clock.sleeps == [2.0]  # the chain waited it out and retried by itself


async def test_a_cooldown_that_is_still_running_makes_the_next_question_wait_not_fail(tmp_path: Path) -> None:
    clock = FakeClock()
    cooldowns = Cooldowns()
    await cooldowns.start(MODEL, 15.0)  # an earlier run's 429
    fake = FakeLLM(default=ok_script())
    redis = await redis_client.get_redis()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        await redis.delete(f"llm:cooldown:{MODEL}")

    parsed = smoke.parse_args(["--model", MODEL, "--lang", "en", "--out", str(tmp_path / "o"), "--pause", "0", "--count", "1"])
    await smoke.run_smoke(parsed, chain=real_chain(fake, clock), sleep=sleep, clock=clock.monotonic)
    assert rows(tmp_path / "o")[0]["outcome"] == "ok"
    assert len(slept) == 1 and 14.0 <= slept[0] <= 17.0
