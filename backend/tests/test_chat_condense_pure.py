"""
tests/test_chat_condense_pure.py
────────────────────────────────
The pure parts of the condense step (step 6a-11; docs/phase6-plan.md 2.1 [D], 5.1; owner decision 1 of the 6a-11 brief):

  - follow-up detection: only a short question (at most FOLLOW_UP_MAX_WORDS words) or one with a referring word (en, mr,
    hi) is a candidate; the caller also needs history (never the first turn);
  - the request: our fixed system prompt, ONE user message holding the masked history (old labels stripped) as data and
    the masked question; no tools (the request type has no field for them), a small output cap, short timeouts;
  - the output is untrusted: one line, length-capped, citation labels ([S#], 【S#】, [S 1]) and verdict words removed,
    a leading "Standalone question:"-style label and wrapping quotes removed; nothing usable left -> None (the caller
    then uses the original question);
  - TenantLLMContext only takes a frozenset of provider names or None.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.llm.chain import TenantLLMContext
from app.llm.types import LLMRequest
from app.services import chat_condense, chat_prompt, pii
from app.services.chat_condense import (
    CONDENSE_MAX_CHARS,
    CONDENSE_MAX_OUTPUT_TOKENS,
    CONDENSE_SYSTEM_PROMPT,
    CONDENSE_TIMEOUT_TOTAL_S,
    FOLLOW_UP_MAX_WORDS,
    build_request,
    clean_output,
    looks_like_follow_up,
)

MASK = pii.masker(own_name="Asha Patil", own_email="asha@example.org")
HISTORY = (chat_prompt.HistoryTurn(question="what is a prop in react", answer="A prop is an input [S1] from the parent [S2]."),)

# ── Follow-up detection ───────────────────────────────────────────────────────


@pytest.mark.parametrize("question", [
    "and state?",
    "why",
    "explain more",
    "what about hooks",
    "one two three four five six",
    "can you explain that again with a longer example please",
    "how does it work when the parent component re-renders",
    "give me an example of this from the notes we covered",
    "what are these used for in a real application today",
    "where do they get their values from in a big application",
    "त्याचे उदाहरण द्या कृपया वर्गाच्या नोट्स मधून आणखी एक",
    "ते कसे काम करते हे समजावून सांगा कृपया आणखी",
    "इसका एक और उदाहरण दीजिए कक्षा के नोट्स से कृपया",
    "वह कैसे काम करता है विस्तार से समझाइए कृपया अभी",
])
def test_a_short_or_referring_question_is_a_follow_up(question: str) -> None:
    assert looks_like_follow_up(question)


@pytest.mark.parametrize("question", [
    "",
    "   ",
    "explain how a parent component passes data down to children",
    "what is the difference between props and state in react components",
    "रिॲक्टमध्ये प्रॉप आणि स्टेट यांच्यातील फरक काय आहे सविस्तर सांगा",
    # a referring word inside another word does not count
    "explain iterators and thistles and thatched roofs in detail please",
])
def test_a_long_self_contained_question_is_not_a_follow_up(question: str) -> None:
    assert not looks_like_follow_up(question)


def test_the_short_question_bound_is_six_words() -> None:
    assert FOLLOW_UP_MAX_WORDS == 6
    assert looks_like_follow_up(" ".join(["word"] * 6))
    assert not looks_like_follow_up(" ".join(["word"] * 7))


# ── The request ───────────────────────────────────────────────────────────────


def _request(
    question: str = "and asha@example.org state?", history: tuple[chat_prompt.HistoryTurn, ...] = HISTORY
) -> LLMRequest:
    return build_request(question=question, history=history, mask=MASK, request_id=str(uuid.uuid4()),
                         settings=settings)


def test_the_request_is_our_prompt_and_one_user_message_with_masked_data() -> None:
    req = _request("and what does Asha Patil asha@example.org call state?")
    assert req.system == CONDENSE_SYSTEM_PROMPT
    (message,) = req.messages
    assert message.role == "user"
    body = message.content
    assert "asha@example.org" not in body and "Asha Patil" not in body
    assert pii.EMAIL in body and pii.NAME in body
    assert "what is a prop in react" in body
    # the labels are stripped exactly as for the answer prompt's history (chat_prompt.strip_labels)
    assert "[S1]" not in body and "[S2]" not in body and "A prop is an input from the parent ." in body
    assert body.rstrip().endswith(f"and what does {pii.NAME} {pii.EMAIL} call state?")
    assert req.max_output_tokens == CONDENSE_MAX_OUTPUT_TOKENS
    assert req.timeouts.total == CONDENSE_TIMEOUT_TOTAL_S
    assert req.timeouts.first_token <= CONDENSE_TIMEOUT_TOTAL_S and req.timeouts.idle <= CONDENSE_TIMEOUT_TOTAL_S


def test_the_request_has_no_field_for_tools() -> None:
    req = _request()
    for name in ("tools", "functions", "tool_choice", "response_format"):
        assert not hasattr(req, name)


def test_the_history_in_the_request_is_capped_like_the_answer_prompt() -> None:
    long_history = tuple(chat_prompt.HistoryTurn(question=f"q{i} " + "x" * 2000, answer=f"a{i} " + "y" * 2000)
                         for i in range(6))
    body = _request(history=long_history).messages[0].content
    assert "q5" in body and "q4" in body and "q3" not in body  # the last CHAT_HISTORY_TURNS exchanges only
    assert body.count("x") <= settings.CHAT_HISTORY_CHARS * settings.CHAT_HISTORY_TURNS
    assert body.count("y") <= settings.CHAT_HISTORY_CHARS * settings.CHAT_HISTORY_TURNS


def test_the_system_prompt_says_the_history_is_data_and_asks_for_one_line() -> None:
    lowered = CONDENSE_SYSTEM_PROMPT.lower()
    assert "data" in lowered and "one line" in lowered and "do not answer" in lowered


# ── The output is untrusted ───────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [
    ("What is state in React?", "What is state in React?"),
    ("  What is state in React?  \n", "What is state in React?"),
    ("\n\nWhat is state in React?\nBecause the student asked about props.", "What is state in React?"),
    ("Standalone question: What is state in React?", "What is state in React?"),
    ("**Rewritten question:** What is state in React?", "What is state in React?"),
    ("Question: What is state in React?", "What is state in React?"),
    ('"What is state in React?"', "What is state in React?"),
    ("What is state [S1] in React [S2, S3]?", "What is state in React?"),
    ("What is state 【S1】 in React?", "What is state in React?"),
    ("What is state [S 1] in React?", "What is state in React?"),
    ("ANSWERABLE What is state in React?", "What is state in React?"),
    ("What is NOT_IN_SOURCES state in React?", "What is state in React?"),
    ("answerable: what is state", "what is state"),
    ("What‮is state​ in React?", "Whatis state in React?"),
])
def test_the_output_is_cleaned_to_one_safe_line(raw: str, expected: str) -> None:
    assert clean_output(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "\n\n", "[S1]", "[S1] [S2]", "【S1】", "ANSWERABLE", "NOT_IN_SOURCES", "**NOT_IN_SOURCES**",
    "Standalone question:", '""', "?!.", "[S1] ANSWERABLE ?",
])
def test_nothing_usable_left_gives_none(raw: str) -> None:
    assert clean_output(raw) is None


def test_the_output_is_capped() -> None:
    out = clean_output("word " * 2000)
    assert out is not None and len(out) <= CONDENSE_MAX_CHARS
    assert CONDENSE_MAX_CHARS <= settings.RETRIEVAL_MAX_QUERY_CHARS


def test_the_module_never_logs_or_prints() -> None:
    import inspect

    source = inspect.getsource(chat_condense)
    assert "logger" not in source and "print(" not in source


# ── TenantLLMContext ──────────────────────────────────────────────────────────


def test_the_tenant_context_takes_a_frozenset_of_provider_names_or_none() -> None:
    tid = uuid.uuid4()
    assert TenantLLMContext(tenant_id=tid).allowed_providers is None
    ctx = TenantLLMContext(tenant_id=tid, allowed_providers=frozenset({"groq"}))
    assert ctx.allows("groq") and not ctx.allows("openrouter") and not ctx.allows("GROQ")
    assert TenantLLMContext(tenant_id=tid).allows("openrouter")
    assert not TenantLLMContext(tenant_id=tid, allowed_providers=frozenset()).allows("groq")
    with pytest.raises(TypeError):
        TenantLLMContext(tenant_id=tid, allowed_providers=["groq"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TenantLLMContext(tenant_id=tid, allowed_providers=frozenset({1}))  # type: ignore[arg-type]
