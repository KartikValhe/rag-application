"""
Step 6a-2 (docs/phase6-plan.md 5.6, 7.1, 7.4): the request, message, event and error types of the LLM client.

The model gets NO tools: the request type has no field for tools, functions, tool_choice or response_format and
cannot be built with one. Errors carry no free text, so a provider response or a key can never end up in one.
"""

import dataclasses

import pytest

from app.llm.errors import (
    LLMAuthError,
    LLMBadRequest,
    LLMContentBlocked,
    LLMError,
    LLMPolicyError,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
    ModelRegistryError,
)
from app.llm.types import (
    DoneEvent,
    LLMEvent,
    LLMMessage,
    LLMRequest,
    LLMTimeouts,
    TokenEvent,
    UsageEvent,
)

pytestmark = pytest.mark.usefixtures("no_network_at_all")

FORBIDDEN_FIELDS = (
    "tools",
    "functions",
    "function_call",
    "tool_choice",
    "tool_calls",
    "parallel_tool_calls",
    "response_format",
)

TIMEOUTS = LLMTimeouts(connect=5.0, first_token=10.0, idle=15.0, total=45.0)


def make_request(**overrides: object) -> LLMRequest:
    fields: dict[str, object] = {
        "model_key": "fake:script",
        "system": "You answer from the sources.",
        "messages": (LLMMessage(role="user", content="When is the exam?"),),
        "max_output_tokens": 600,
        "timeouts": TIMEOUTS,
        "request_id": "req-1",
    }
    fields.update(overrides)
    return LLMRequest(**fields)  # type: ignore[arg-type]


# ── the model gets no tools ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", FORBIDDEN_FIELDS)
def test_a_request_cannot_be_built_with_tool_fields(name: str) -> None:
    with pytest.raises(TypeError):
        make_request(**{name: [{"type": "function"}]})


@pytest.mark.parametrize("name", FORBIDDEN_FIELDS)
def test_a_message_cannot_carry_tool_fields(name: str) -> None:
    with pytest.raises(TypeError):
        LLMMessage(role="user", content="x", **{name: []})  # type: ignore[arg-type]


def test_no_field_of_the_request_or_message_is_about_tools() -> None:
    names = {f.name for f in dataclasses.fields(LLMRequest)} | {f.name for f in dataclasses.fields(LLMMessage)}
    assert not names & set(FORBIDDEN_FIELDS)
    for name in names:
        assert "tool" not in name and "function" not in name and "response_format" not in name


def test_the_request_cannot_grow_a_tool_attribute_later() -> None:
    req = make_request()
    # frozen: FrozenInstanceError (an AttributeError); an unknown name on a slotted frozen dataclass raises TypeError
    # on CPython 3.12 (a known quirk of the generated __setattr__). Either way nothing is set.
    with pytest.raises((AttributeError, TypeError)):
        req.tools = []  # type: ignore[attr-defined]
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.system = "changed"  # type: ignore[misc]
    assert not hasattr(req, "tools")
    assert not hasattr(req, "__dict__")  # slots: no place to hang an extra attribute


@pytest.mark.parametrize("role", ["tool", "function", "system", "developer", "", "User"])
def test_only_user_and_assistant_messages_exist(role: str) -> None:
    with pytest.raises(ValueError, match="role"):
        LLMMessage(role=role, content="x")  # type: ignore[arg-type]


# ── request validation ───────────────────────────────────────────────────────────────────────────────


def test_a_request_has_the_plan_defaults() -> None:
    req = make_request()
    assert req.temperature == 0.2
    assert req.messages[0].role == "user"
    assert req.timeouts == TIMEOUTS


@pytest.mark.parametrize(
    "bad",
    [
        {"model_key": ""},
        {"system": ""},
        {"messages": ()},
        {"messages": [LLMMessage(role="user", content="x")]},  # a list: the request must stay immutable
        {"max_output_tokens": 0},
        {"max_output_tokens": -5},
        {"max_output_tokens": True},
        {"max_output_tokens": 1.5},
        {"temperature": -0.1},
        {"temperature": 2.5},
        {"temperature": float("nan")},
        {"request_id": ""},
    ],
)
def test_nonsense_requests_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises((ValueError, TypeError)):
        make_request(**bad)


def test_message_content_must_be_text_and_not_empty() -> None:
    for bad in ("", None, 5, b"bytes"):
        with pytest.raises((ValueError, TypeError)):
            LLMMessage(role="user", content=bad)  # type: ignore[arg-type]


def test_the_repr_never_shows_what_the_student_asked_or_the_answer() -> None:
    canary = "QUESTION-CANARY-7731"
    req = make_request(
        system="SYSTEM-CANARY-1180",
        messages=(LLMMessage(role="user", content=canary),),
    )
    shown = repr(req) + str(req) + repr(req.messages[0]) + repr(TokenEvent(text="ANSWER-CANARY-9052"))
    assert canary not in shown
    assert "SYSTEM-CANARY-1180" not in shown
    assert "ANSWER-CANARY-9052" not in shown


# ── timeouts ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        {"connect": 0.0},
        {"first_token": -1.0},
        {"idle": 0.0},
        {"total": float("inf")},
        {"total": float("nan")},
        {"first_token": 50.0},  # above the total: it could never fire
        {"idle": 46.0},
    ],
)
def test_nonsense_timeouts_are_refused(bad: dict[str, float]) -> None:
    fields = {"connect": 5.0, "first_token": 10.0, "idle": 15.0, "total": 45.0, **bad}
    with pytest.raises(ValueError):
        LLMTimeouts(**fields)


# ── events ───────────────────────────────────────────────────────────────────────────────────────────


def test_the_three_event_kinds_are_immutable() -> None:
    events: list[LLMEvent] = [
        TokenEvent(text="a"),
        UsageEvent(prompt_tokens=10, completion_tokens=2, estimated=False),
        DoneEvent(finish_reason="stop"),
    ]
    for event in events:
        with pytest.raises((AttributeError, TypeError)):  # see the note in the request test above
            event.anything = 1  # type: ignore[attr-defined]
        assert not hasattr(event, "anything")


@pytest.mark.parametrize("finish", ["tool_calls", "function_call", "error", "", "STOP"])
def test_a_finish_reason_outside_the_plan_is_refused(finish: str) -> None:
    with pytest.raises(ValueError):
        DoneEvent(finish_reason=finish)  # type: ignore[arg-type]


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_the_two_finish_reasons_are_accepted(finish: str) -> None:
    assert DoneEvent(finish_reason=finish).finish_reason == finish  # type: ignore[arg-type]


def test_usage_needs_the_estimated_flag_and_sane_numbers() -> None:
    with pytest.raises(TypeError):
        UsageEvent(prompt_tokens=1, completion_tokens=1)  # type: ignore[call-arg]
    for bad in (-1, True, 1.5):
        with pytest.raises((ValueError, TypeError)):
            UsageEvent(prompt_tokens=bad, completion_tokens=1, estimated=True)  # type: ignore[arg-type]
        with pytest.raises((ValueError, TypeError)):
            UsageEvent(prompt_tokens=1, completion_tokens=bad, estimated=True)  # type: ignore[arg-type]


def test_a_token_event_needs_text() -> None:
    with pytest.raises(TypeError):
        TokenEvent(text=None)  # type: ignore[arg-type]
    assert TokenEvent(text="").text == ""  # an empty chunk is legal on the wire; the filter drops it


# ── errors ───────────────────────────────────────────────────────────────────────────────────────────

ERRORS: tuple[tuple[type[LLMError], bool], ...] = (
    (LLMRateLimited, True),
    (LLMUnavailable, True),
    (LLMTimeout, True),
    (LLMBadRequest, False),
    (LLMContentBlocked, False),
    (LLMAuthError, False),
)


@pytest.mark.parametrize(("cls", "falls_back"), ERRORS)
def test_the_chain_falls_back_only_where_plan_7_4_says(cls: type[LLMError], falls_back: bool) -> None:
    assert issubclass(cls, LLMError)
    assert cls.falls_back is falls_back
    assert cls().falls_back is falls_back


@pytest.mark.parametrize(("cls", "_"), ERRORS)
def test_errors_take_no_free_text(cls: type[LLMError], _: bool) -> None:
    with pytest.raises(TypeError):
        cls("sk-live-1234 and the provider's whole response body")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        cls(message="x")  # type: ignore[call-arg]


@pytest.mark.parametrize(("cls", "_"), ERRORS)
def test_the_text_of_an_error_is_fixed_and_names_only_provider_and_model(cls: type[LLMError], _: bool) -> None:
    plain = cls()
    named = cls(provider="groq", model_key="groq:llama-3.3-70b-versatile")
    assert str(plain) and str(named).startswith(str(plain))
    assert "groq" in str(named)
    assert named.provider == "groq" and named.model_key == "groq:llama-3.3-70b-versatile"
    assert plain.provider is None and plain.model_key is None


@pytest.mark.parametrize(("cls", "_"), ERRORS)
@pytest.mark.parametrize("bad", ["sk-live key=abc", "a b", "line\nbreak", "x" * 200, "", "Bearer abc"])
def test_a_provider_or_model_name_with_odd_characters_is_refused(cls: type[LLMError], _: bool, bad: str) -> None:
    with pytest.raises(ValueError):
        cls(provider=bad)
    with pytest.raises(ValueError):
        cls(model_key=bad)


def test_rate_limited_carries_retry_after() -> None:
    assert LLMRateLimited(retry_after_s=3.5).retry_after_s == 3.5
    assert LLMRateLimited().retry_after_s is None
    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            LLMRateLimited(retry_after_s=bad)
    # only the rate-limit error has the field
    with pytest.raises(TypeError):
        LLMTimeout(retry_after_s=1.0)  # type: ignore[call-arg]


def test_a_startup_problem_is_not_a_runtime_llm_error() -> None:
    # the chain catches LLMError to fall back; a configuration problem must never be swallowed there
    assert not issubclass(LLMPolicyError, LLMError)
    assert issubclass(ModelRegistryError, LLMPolicyError)
    error = LLMPolicyError(("first problem", "second problem"))
    assert error.problems == ("first problem", "second problem")
    assert "first problem" in str(error) and "second problem" in str(error)
