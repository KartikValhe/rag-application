"""
Step 6a-10c: the tuning settings that 6a-10a left as constants (plan 4.3, 4.5 rule 2, 5.1, 5.5, 7.5), the RAG flag,
and the named constants of the wired turn. Fast: no database, no Redis, no model.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services import chat_prompt, rag_answer

ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

DEFAULTS = {
    "CHAT_CONTEXT_MAX_TOKENS": 2400,  # plan 4.3
    "CHAT_HISTORY_TURNS": 2,  # plan 5.1
    "CHAT_HISTORY_CHARS": 600,  # plan 5.1
    "CHAT_FLAGGED_SOURCES": "mark",  # plan 5.5, Q6 decided
    "CHAT_REQUIRE_CITATION": True,  # plan 4.5 rule 2
    "CHAT_FALLBACK_RETRIEVAL_ONLY": True,  # plan 7.5, Q7 decided
    "CHAT_RAG_ENABLED": False,  # owner, 6a-10 brief answer 2: stays off by default
}


@pytest.mark.parametrize(("name", "value"), DEFAULTS.items())
def test_the_default_is_the_plan_value(name: str, value: object) -> None:
    assert getattr(Settings(), name) == value


def test_the_defaults_match_the_prompt_builder_constants() -> None:
    s = Settings()
    assert s.CHAT_CONTEXT_MAX_TOKENS == chat_prompt.CONTEXT_MAX_TOKENS
    assert s.CHAT_HISTORY_TURNS == chat_prompt.HISTORY_TURNS
    assert s.CHAT_HISTORY_CHARS == chat_prompt.HISTORY_CHARS


@pytest.mark.parametrize("name", DEFAULTS)
def test_every_setting_is_documented_in_env_example(name: str) -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(rf"^{name}=", text, flags=re.MULTILINE), f"{name} is missing from .env.example"


def test_the_rag_flag_is_accepted_now() -> None:
    assert Settings(CHAT_RAG_ENABLED=True).CHAT_RAG_ENABLED is True


@pytest.mark.parametrize(("name", "value"), [
    ("CHAT_FLAGGED_SOURCES", "drop"), ("CHAT_CONTEXT_MAX_TOKENS", 0), ("CHAT_HISTORY_TURNS", -1),
    ("CHAT_HISTORY_CHARS", 0),
])
def test_out_of_range_values_are_refused(name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(**{name: value})  # type: ignore[arg-type]


def test_the_refusal_usage_constant_is_the_owner_value() -> None:
    """Owner decision (6a-10 brief, answer 3): 128, from the 6a-6 runs (refusals reported 66-95 completion tokens)."""
    assert rag_answer.REFUSAL_COMPLETION_TOKENS_ESTIMATE == 128


def test_the_retrieval_only_text_carries_our_sentence_and_the_labels_only() -> None:
    assert rag_answer.retrieval_only_text("en", ("S1", "S2", "S3")) == (
        f"{rag_answer.RETRIEVAL_ONLY_TEXTS['en']}\n\n[S1] [S2] [S3]")
    assert rag_answer.retrieval_only_text("hi", ("S1",)).startswith(rag_answer.RETRIEVAL_ONLY_TEXTS["en"])
    assert rag_answer.refusal_text("hi") == rag_answer.NOT_FOUND_TEXTS["hi"]
    assert rag_answer.refusal_text("xx") == rag_answer.NOT_FOUND_TEXTS["en"]


def test_labels_cited_in_a_text_are_the_table_labels_of_released_groups() -> None:
    table = ("S1", "S2", "S3")
    assert rag_answer.labels_cited_in("a [S2] b [S1, S3] c [S2]", table) == ("S2", "S1", "S3")
    assert rag_answer.labels_cited_in("[S 1] (S1) [S9] [S1e3]", table) == ()
