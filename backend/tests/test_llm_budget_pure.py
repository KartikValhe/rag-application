"""
Step 6a-5 (docs/phase6-plan.md 7.7): the parts of the token budget that need no Redis.

  * the token estimate when a provider reports nothing: ceil(chars / 3) Latin, ceil(chars / 2) Devanagari. The ratios
    are UNVERIFIED (6a-6 measures them), so they are settings, never numbers in the code;
  * the daily limits: tenant 300,000 and platform 2,000,000 are PLACEHOLDERS ("set after Q1"), settings only;
  * the tenant override rule: NULL = the default, above the platform ceiling = refused;
  * the Redis key names and the UTC day.
"""

from __future__ import annotations

import ast
import inspect
import re
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from app.core.config import Settings, settings
from app.llm.types import LLMMessage, LLMRequest, LLMTimeouts
from app.services import llm_budget, llm_usage
from app.services.llm_budget import (
    COUNTER_TTL_SECONDS,
    BudgetOverrideRefused,
    check_budget_override,
    effective_tenant_limit,
    estimate_prompt_tokens,
    estimate_text_tokens,
    platform_day_key,
    reservation_amount,
    tenant_day_key,
    utc_day,
)

BUDGET_NAMES = (
    "LLM_TENANT_DAILY_TOKENS",
    "LLM_PLATFORM_DAILY_TOKENS",
    "LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN",
    "LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI",
)
BACKEND = Path(__file__).resolve().parent.parent


def fresh(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    for name in BUDGET_NAMES:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


# ── settings ─────────────────────────────────────────────────────────────────────────────────────────


def test_defaults_are_the_plan_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    s = fresh(monkeypatch)
    assert s.LLM_TENANT_DAILY_TOKENS == 300_000
    assert s.LLM_PLATFORM_DAILY_TOKENS == 2_000_000
    assert s.LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN == 3.0
    assert s.LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI == 2.0


def test_the_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BUDGET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_TENANT_DAILY_TOKENS", "5000")
    monkeypatch.setenv("LLM_PLATFORM_DAILY_TOKENS", "90000")
    monkeypatch.setenv("LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN", "3.6")
    monkeypatch.setenv("LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI", "1.5")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.LLM_TENANT_DAILY_TOKENS, s.LLM_PLATFORM_DAILY_TOKENS) == (5000, 90000)
    assert (s.LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN, s.LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI) == (3.6, 1.5)


@pytest.mark.parametrize(
    "bad",
    [
        {"LLM_TENANT_DAILY_TOKENS": 0},
        {"LLM_PLATFORM_DAILY_TOKENS": -1},
        {"LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN": 0},
        {"LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI": -2},
        {"LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN": 100},
        # the tenant default may not be above the platform ceiling
        {"LLM_TENANT_DAILY_TOKENS": 3_000_000},
    ],
)
def test_nonsense_values_are_refused_at_start_up(bad: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        fresh(monkeypatch, **bad)


def test_env_example_lists_the_budget_settings_marked_set_after_q1() -> None:
    text = (BACKEND / ".env.example").read_text(encoding="utf-8")
    lines = text.splitlines()
    for name in BUDGET_NAMES:
        assert any(re.match(rf"^#?\s*{name}=", line) for line in lines), f"{name} is missing from .env.example"
    for name in ("LLM_TENANT_DAILY_TOKENS", "LLM_PLATFORM_DAILY_TOKENS"):
        at = next(i for i, line in enumerate(lines) if line.startswith(f"{name}="))
        comment = " ".join(lines[max(0, at - 4) : at]).lower()
        assert "set after q1" in comment, f"{name} must be marked 'set after Q1' in .env.example"
    for name in ("LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN", "LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI"):
        at = next(i for i, line in enumerate(lines) if line.startswith(f"{name}="))
        comment = " ".join(lines[max(0, at - 4) : at]).upper()
        assert "UNVERIFIED" in comment, f"{name} must be marked UNVERIFIED in .env.example"


def test_config_marks_the_token_defaults_set_after_q1() -> None:
    source = (BACKEND / "app" / "core" / "config.py").read_text(encoding="utf-8")
    for name in ("LLM_TENANT_DAILY_TOKENS", "LLM_PLATFORM_DAILY_TOKENS"):
        at = source.index(f"    {name}:")
        assert "set after Q1" in source[max(0, at - 600) : at], f"{name}: no 'set after Q1' comment above it"


@pytest.mark.parametrize("module", [llm_budget, llm_usage])
def test_no_budget_number_or_ratio_is_hard_coded(module: object) -> None:
    """The placeholders and the UNVERIFIED ratios live in settings only, so Q1 and 6a-6 change them without code edits."""
    source = inspect.getsource(module)  # type: ignore[arg-type]
    tree = ast.parse(source)
    numbers = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, int | float)}
    assert not numbers & {300_000, 2_000_000}, f"a budget placeholder is hard-coded in {getattr(module, '__name__', module)}"
    # No division by a literal 2 or 3 anywhere: the ratios come from settings.
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div | ast.FloorDiv):
            right = node.right
            assert not (isinstance(right, ast.Constant) and right.value in (2, 3)), "a chars-per-token ratio is hard-coded"
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "Fraction":
            assert not any(isinstance(a, ast.Constant) and a.value in (2, 3, "2", "3") for a in node.args), (
                "a chars-per-token ratio is hard-coded"
            )


# ── the estimate ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", 0),
        ("a", 1),
        ("abc", 1),
        ("abcd", 2),
        ("x" * 300, 100),
        ("x" * 301, 101),
        ("नमस्ते", 3),  # 6 code points, all Devanagari: ceil(6 / 2)
        ("प्रकाशसंश्लेषण", 7),  # 14 code points
        ("abc नमस्ते", 5),  # mixed: ceil(4 / 3 + 6 / 2) = ceil(4.33); the space counts with the Latin side
    ],
)
def test_estimate_text_tokens(text: str, expected: int) -> None:
    assert estimate_text_tokens(text) == expected


def test_the_ratios_are_read_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN", 4.0)
    monkeypatch.setattr(settings, "LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI", 2.5)
    assert estimate_text_tokens("x" * 8) == 2
    assert estimate_text_tokens("x" * 9) == 3
    assert estimate_text_tokens("न" * 5) == 2  # exactly 5 / 2.5, no float rounding up
    assert estimate_text_tokens("न" * 6) == 3


def test_extended_devanagari_counts_as_devanagari() -> None:
    assert estimate_text_tokens("ꣲꣳ") == 1  # Devanagari Extended: ceil(2 / 2)


def _req(system: str, *contents: str, max_out: int = 600) -> LLMRequest:
    return LLMRequest(
        model_key="groq:openai/gpt-oss-120b",
        system=system,
        messages=tuple(LLMMessage(role="user" if i % 2 == 0 else "assistant", content=c) for i, c in enumerate(contents)),
        max_output_tokens=max_out,
        temperature=0.2,
        timeouts=LLMTimeouts(connect=5, first_token=10, idle=15, total=45),
        request_id="req-1",
    )


def test_the_prompt_estimate_covers_the_system_prompt_and_every_message() -> None:
    req = _req("s" * 30, "q" * 9, "a" * 3, "f" * 1)
    assert estimate_prompt_tokens(req) == 10 + 3 + 1 + 1
    assert reservation_amount(req) == 15 + 600


def test_reservation_amount_uses_max_output_tokens() -> None:
    assert reservation_amount(_req("s", "abc", max_out=42)) == 1 + 1 + 42


# ── limits and the override rule ─────────────────────────────────────────────────────────────────────


def test_no_override_means_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_TENANT_DAILY_TOKENS", 1000)
    monkeypatch.setattr(settings, "LLM_PLATFORM_DAILY_TOKENS", 5000)
    assert effective_tenant_limit(None) == 1000
    assert effective_tenant_limit(200) == 200
    assert effective_tenant_limit(4000) == 4000
    assert effective_tenant_limit(5000) == 5000


def test_an_override_above_the_platform_ceiling_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value that slipped in before the ceiling was lowered: not applied (the default is), and logged."""
    monkeypatch.setattr(settings, "LLM_TENANT_DAILY_TOKENS", 1000)
    monkeypatch.setattr(settings, "LLM_PLATFORM_DAILY_TOKENS", 5000)
    with capture_logs() as logs:
        assert effective_tenant_limit(5001) == 1000
    assert [e["event"] for e in logs] == ["llm_budget_override_refused"]
    assert logs[0]["log_level"] == "warning"


def test_check_budget_override_for_the_settings_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_PLATFORM_DAILY_TOKENS", 5000)
    check_budget_override(None)
    check_budget_override(1)
    check_budget_override(5000)
    with pytest.raises(BudgetOverrideRefused) as caught:
        check_budget_override(5001)
    assert caught.value.code == "token_budget_above_platform_ceiling"
    for bad in (0, -1, True, 1.5, "100"):
        with pytest.raises(BudgetOverrideRefused) as caught:
            check_budget_override(bad)  # type: ignore[arg-type]
        assert caught.value.code == "token_budget_invalid"


# ── keys and the day ─────────────────────────────────────────────────────────────────────────────────


def test_the_keys_are_the_plan_keys() -> None:
    tid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    assert tenant_day_key(tid, date(2026, 11, 25)) == "usage:t:11111111-2222-3333-4444-555555555555:d:20261125:tokens"
    assert platform_day_key(date(2026, 1, 5)) == "usage:platform:d:20260105:tokens"


def test_the_ttl_is_48_hours() -> None:
    assert COUNTER_TTL_SECONDS == 48 * 3600


def test_the_day_is_the_utc_day() -> None:
    assert utc_day(datetime(2026, 11, 25, 23, 59, 59, tzinfo=UTC)) == date(2026, 11, 25)
    ist = timezone(timedelta(hours=5, minutes=30))
    assert utc_day(datetime(2026, 11, 26, 3, 0, tzinfo=ist)) == date(2026, 11, 25)  # 21:30 UTC the day before
    with pytest.raises(ValueError):
        utc_day(datetime(2026, 11, 25, 12, 0))  # a naive time has no day
