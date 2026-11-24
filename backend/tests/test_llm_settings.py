"""
Step 6a-2 (docs/phase6-plan.md 7.4, 7.6, 5.6): the LLM and chat settings, and the provider keys as SecretStr.

Neither the shell environment nor a .env file may change what these tests see.
"""

import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.llm.types import LLMTimeouts

pytestmark = pytest.mark.usefixtures("no_network_at_all")

NEW_NAMES = (
    "LLM_ANSWER_CHAIN",
    "LLM_CONDENSE_CHAIN",  # step 6a-4
    "LLM_ZDR_CONFIRMED",
    "LLM_ALLOW_TRAINING_PROVIDERS",
    "LLM_TIMEOUT_CONNECT",
    "LLM_TIMEOUT_FIRST_TOKEN",
    "LLM_TIMEOUT_IDLE",
    "LLM_TIMEOUT_TOTAL",
    "CHAT_MAX_OUTPUT_TOKENS",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
)
KEY_NAMES = ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY")


def fresh(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    for name in NEW_NAMES:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_defaults_are_the_plan_values(monkeypatch: pytest.MonkeyPatch) -> None:
    s = fresh(monkeypatch)
    assert (s.LLM_TIMEOUT_CONNECT, s.LLM_TIMEOUT_FIRST_TOKEN, s.LLM_TIMEOUT_IDLE, s.LLM_TIMEOUT_TOTAL) == (5, 10, 15, 45)
    assert s.CHAT_MAX_OUTPUT_TOKENS == 600
    assert s.LLM_ZDR_CONFIRMED is False  # stays false until the owner checks the Groq console (6a-6)
    assert s.LLM_ALLOW_TRAINING_PROVIDERS is False
    assert s.LLM_ANSWER_CHAIN == ""
    assert s.llm_answer_chain == ()  # nothing configured: no provider is ever called


def test_the_chain_is_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    s = fresh(monkeypatch, LLM_ANSWER_CHAIN=" groq:a-model , openrouter:vendor/m:free ,, ")
    assert s.llm_answer_chain == ("groq:a-model", "openrouter:vendor/m:free")


def test_the_condense_chain_is_empty_by_default_and_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # step 6a-4: empty means "no condense" (the caller uses the raw question), never an error
    assert fresh(monkeypatch).llm_condense_chain == ()
    s = fresh(monkeypatch, LLM_CONDENSE_CHAIN=" groq:small ,, openrouter:vendor/s:free ")
    assert s.llm_condense_chain == ("groq:small", "openrouter:vendor/s:free")


def test_the_chain_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NEW_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_ANSWER_CHAIN", "groq:x")
    monkeypatch.setenv("LLM_ZDR_CONFIRMED", "true")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.llm_answer_chain == ("groq:x",) and s.LLM_ZDR_CONFIRMED is True


@pytest.mark.parametrize(
    "bad",
    [
        {"LLM_TIMEOUT_CONNECT": 0},
        {"LLM_TIMEOUT_FIRST_TOKEN": -1},
        {"LLM_TIMEOUT_IDLE": 0},
        {"LLM_TIMEOUT_TOTAL": 0},
        {"LLM_TIMEOUT_FIRST_TOKEN": 60, "LLM_TIMEOUT_TOTAL": 45},  # a stage longer than the whole answer
        {"LLM_TIMEOUT_IDLE": 50, "LLM_TIMEOUT_TOTAL": 45},
        {"CHAT_MAX_OUTPUT_TOKENS": 0},
        {"CHAT_MAX_OUTPUT_TOKENS": 100_000},
    ],
)
def test_nonsense_values_are_refused_at_start_up(bad: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        fresh(monkeypatch, **bad)


def test_the_timeouts_become_one_value_object(monkeypatch: pytest.MonkeyPatch) -> None:
    s = fresh(monkeypatch, LLM_TIMEOUT_FIRST_TOKEN=8)
    assert LLMTimeouts.from_settings(s) == LLMTimeouts(connect=5, first_token=8, idle=15, total=45)


# ── secrets ──────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", KEY_NAMES)
def test_a_key_is_empty_by_default_and_a_secret_str(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    value = getattr(fresh(monkeypatch), name)
    assert isinstance(value, SecretStr) and value.get_secret_value() == ""


@pytest.mark.parametrize("name", KEY_NAMES)
def test_a_key_never_shows_in_repr_str_dump_or_json(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    canary = "CANARY-KEY-" + "9f3a1c7e" * 4
    s = fresh(monkeypatch, **{name: canary})
    assert getattr(s, name).get_secret_value() == canary
    shown = [
        repr(s),
        str(s),
        repr(getattr(s, name)),
        str(getattr(s, name)),
        str(s.model_dump()),
        s.model_dump_json(),
        repr(s.model_fields_set),
        f"{s!r} {s!s}",
    ]
    assert not [text for text in shown if canary in text]


@pytest.mark.parametrize("name", KEY_NAMES)
def test_a_key_does_not_leak_through_a_validation_error(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    canary = "CANARY-KEY-" + "5b2d8e04" * 4
    with pytest.raises(ValueError) as caught:
        fresh(monkeypatch, **{name: canary, "LLM_TIMEOUT_TOTAL": 0})
    assert canary not in str(caught.value) and canary not in repr(caught.value)

    with pytest.raises(ValueError) as caught_after:
        fresh(monkeypatch, **{name: canary, "LLM_TIMEOUT_FIRST_TOKEN": 99})  # fails the cross-field check
    assert canary not in str(caught_after.value) and canary not in repr(caught_after.value)


@pytest.mark.parametrize("name", KEY_NAMES)
def test_a_key_is_read_from_the_environment_and_stays_hidden(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    canary = "CANARY-ENV-" + "c41e7a90" * 4
    for other in NEW_NAMES:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, canary)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert getattr(s, name).get_secret_value() == canary
    assert canary not in repr(s) and canary not in s.model_dump_json()


def test_the_app_never_reads_a_key_value_outside_the_llm_package() -> None:
    # only an adapter (6a-3, inside app/llm) may call get_secret_value() on a provider key
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"(GROQ|OPENROUTER|GEMINI)_API_KEY", text) and "app/llm/" not in path.as_posix() and path.name != "config.py":
            offenders.append(path.relative_to(app_dir).as_posix())
    assert offenders == []


# ── .env.example ─────────────────────────────────────────────────────────────────────────────────────


def test_env_example_lists_every_new_setting_and_holds_no_key_value() -> None:
    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for name in NEW_NAMES:
        assert re.search(rf"^#?\s*{name}=", text, re.MULTILINE), f"{name} is missing from .env.example"
    for name in KEY_NAMES:
        match = re.search(rf"^#?\s*{name}=(.*)$", text, re.MULTILINE)
        assert match is not None and match.group(1).strip() == "", f"{name} must be empty in .env.example"
    assert not re.search(r"\b(gsk_|sk-or-|AIza)[A-Za-z0-9_-]{8,}", text)
