"""
Step 6a-10a: the Phase 7 checkpoints (docs/phase6-plan.md 10.3). Phase 6 ships two real checks (input length, no
context) and leaves the rest as no-ops. Pure module.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import guardrails as g
from tests.fixtures.chat_hits import make_hit

MODULE = Path(g.__file__)


def ctx(question: str = "What is a prop?", hits: tuple[object, ...] = ()) -> g.GuardContext:
    return g.GuardContext(question=question, language="en", hits=hits)  # type: ignore[arg-type]


def test_the_input_checkpoint_passes_an_ordinary_question() -> None:
    assert g.GuardrailPipeline().check_input(ctx()) == g.PASS


def test_the_input_checkpoint_refuses_a_question_over_the_limit() -> None:
    pipe = g.GuardrailPipeline(max_input_chars=10)
    assert pipe.check_input(ctx("x" * 10)) == g.PASS
    assert pipe.check_input(ctx("x" * 11)) == g.GuardResult(allowed=False, check="input_length", code="message_too_long")


def test_the_output_checkpoint_refuses_an_answer_written_without_any_source() -> None:
    pipe = g.GuardrailPipeline()
    assert pipe.check_output(ctx(hits=()), "Some answer") == g.GuardResult(
        allowed=False, check="no_context", code="no_context")
    assert pipe.check_output(ctx(hits=(make_hit(1),)), "Some answer") == g.PASS


def test_extra_checks_run_in_order_and_the_first_refusal_wins() -> None:
    calls: list[str] = []

    def first(c: g.GuardContext, text: str | None) -> g.GuardResult:
        calls.append("first")
        return g.GuardResult(allowed=False, check="first", code="first")

    def second(c: g.GuardContext, text: str | None) -> g.GuardResult:
        calls.append("second")
        return g.PASS

    pipe = g.GuardrailPipeline(extra_input=(second, first, second))
    assert pipe.check_input(ctx()).check == "first"
    assert calls == ["second", "first"]


def test_the_default_input_limit_is_the_chat_limit() -> None:
    assert g.DEFAULT_MAX_INPUT_CHARS == 500


def test_the_module_is_pure() -> None:
    allowed = {"__future__", "dataclasses", "typing", "collections.abc"}
    found: set[str] = set()
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    assert found <= allowed, sorted(found - allowed)
