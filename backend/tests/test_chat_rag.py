"""
tests/test_chat_rag.py
──────────────────────
The wired RAG turn (step 6a-10c; docs/phase6-plan.md 2.1, 4, 5, 6, 7.4-7.7, 8.3, 8.6, 9.1, 11.1-11.3) through the full
app stack, with the REAL roles (T1, T2 and T3 run as app_tenant through chat_session_context). Every provider is the
FakeLLM or the Groq adapter replaying one of the 8 real recordings; the query embedder is scripted. No network, no key.

Rules checked here:
  - the question is embedded with NO connection checked out, then searched in a short T2 with that vector only;
  - nothing above the threshold: the fixed refusal of 4.4 in the question's language and NO model call;
  - the model's text passes the 6a-10a filter: no verdict line, no invented label, NOT_IN_SOURCES never shows model
    text and closes the provider stream at once; an answer with no valid label is retracted (replace);
  - citations are built by the server from this turn's hits only, and students get the student fields only;
  - the budget is reserved before the call and settled to the provider-reported counts (hidden reasoning included),
    a closed-early refusal to the estimate with REFUSAL_COMPLETION_TOKENS_ESTIMATE; over budget the retrieval-only
    answer (no call), every model failing the fallback answer;
  - finish_reason length is truncated, a mid-stream failure interrupted, the stream cap interrupted;
  - T3 writes the row, the citation rows and the usage row in ONE transaction;
  - a retry never makes a second answer row; a disconnect at every phase leaves one row (cancelled, the text the
    client got), no slot, no connection, and a usage row once the model had answered; is_disconnected() is checked
    between tokens; no question, source or answer text in any log.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import slots
from app.core.config import settings
from app.db.engine import engine as app_engine
from app.llm.errors import LLMBadRequest, LLMRateLimited, LLMUnavailable
from app.llm.fake import Script
from app.llm.types import UsageEvent
from app.main import app
from app.services import (
    chat_messages,
    chat_prompt,
    chat_sse,
    llm_budget,
    llm_usage,
    rag_answer,
    retrieval,
)
from app.services.chat_stream_filter import normalise_chunk
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.chat_world import conversation, messages_path, raw_json, rows
from tests.fixtures.documents import auth
from tests.fixtures.rag_world import (
    ANSWER_MODEL,
    KEY_CANARY,
    PROPS_TEXT,
    SOURCE_CANARY,
    STATE_TEXT,
    STUDENT_CITATION_KEYS,
    Gate,
    RagSwitch,
    checked_out,
    citation_rows,
    fake,
    rag_fixture,  # noqa: F401  (the `rag` pytest fixture)
    read_recording,
    scripted,
    seed_canaries,
    seed_in_scope,
    tenant_tokens,
    usage_rows,
)
from tests.fixtures.sse_client import SseResult, stream_request

pytestmark = pytest.mark.usefixtures("fixed_clock")

QUESTION = "what is a prop in react"
MR_QUESTION = "रिॲक्टमध्ये प्रॉप म्हणजे काय आहे"
ANSWER_TOKENS = ("ANSWER", "ABLE", "\n", "A prop is an input", " [", "S", "1", "]", " and state is local [S2].")
ANSWER_TEXT = "A prop is an input [S1] and state is local [S2]."
REFUSAL_EN = rag_answer.NOT_FOUND_TEXTS["en"]
REFUSAL_MR = rag_answer.NOT_FOUND_TEXTS["mr"]


async def ask(w: WorldTenant, user: Any, conv_id: uuid.UUID, content: str = QUESTION, **kw: Any) -> SseResult:
    return await stream_request(app, "POST", messages_path(conv_id), headers=auth(w, user),
                                body=raw_json(content, kw.pop("client_request_id", None)), **kw)


def tokens_of(result: SseResult) -> str:
    return "".join(e.data["text"] for e in result.events if e.name == "token")


def client_text(result: SseResult) -> str:
    """The text the client ends with: the tokens, or what a replace event put in their place."""
    text = ""
    for e in result.events:
        if e.name == "token":
            text += e.data["text"]
        elif e.name == "replace":
            text = e.data["text"]
    return text


async def _slots(w: WorldTenant, user_id: uuid.UUID) -> tuple[int, int]:
    return (await slots.default_manager.count(f"{chat_messages.ACTIVE_USER_KEY_PREFIX}{user_id}"),
            await slots.default_manager.count(f"{chat_messages.ACTIVE_TENANT_KEY_PREFIX}{w.id}"))


async def assert_clean(w: WorldTenant, user_id: uuid.UUID) -> None:
    assert await _slots(w, user_id) == (0, 0)
    assert checked_out() == 0


@pytest.fixture
async def material(academic_world: AcademicWorld, raw_db: AsyncSession) -> Any:
    return await seed_in_scope(raw_db, academic_world.a)


# ── The not-found path ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("question", "text", "language"), [(QUESTION, REFUSAL_EN, "en"), (MR_QUESTION, REFUSAL_MR, "mr")])
async def test_nothing_above_the_threshold_gives_the_fixed_text_and_no_model_call(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, question: str, text: str, language: str
) -> None:
    w = academic_world.a
    await seed_in_scope(raw_db, w, sims=(0.5, 0.4))
    provider = rag.use(fake(*ANSWER_TOKENS))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id, question)
    assert result.names == ["meta", "token", "done"]
    assert result.first("meta")["route"] == "rag" and result.first("meta")["language"] == language
    assert tokens_of(result) == text
    assert result.first("done") == {"outcome": "not_found", "partial": False,
                                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False},
                                    "latency_ms": result.first("done")["latency_ms"]}
    assert provider.requests == [] and rag.embed.queries == [question]
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content, answer.route, answer.partial) == ("not_found", text, "rag", False)
    assert answer.retrieval is not None and answer.retrieval["reason"] == "below_threshold"
    assert answer.retrieval["chunk_ids"] == [] and answer.provider is None and answer.prompt_tokens is None
    assert await citation_rows(raw_db, answer.id) == [] and await usage_rows(raw_db, w.id) == []
    assert await tenant_tokens(w.id) == 0
    await assert_clean(w, w.s1.id)


def test_the_fixed_refusal_texts_are_the_plan_texts() -> None:
    assert rag_answer.NOT_FOUND_TEXTS["en"] == (
        "I couldn't find this in your class material. Try asking in a different way, or ask your teacher.")
    assert rag_answer.NOT_FOUND_TEXTS["mr"] == (
        "हे तुमच्या वर्गाच्या साहित्यात सापडले नाही. प्रश्न वेगळ्या शब्दांत विचारा किंवा तुमच्या शिक्षकांना विचारा.")
    assert rag_answer.RETRIEVAL_ONLY_TEXTS["en"] == (
        "I can't write an answer right now. These parts of your class material matched your question best; they "
        "may not answer it fully.")
    assert rag_answer.REFUSAL_COMPLETION_TOKENS_ESTIMATE == 128


# ── An answer ─────────────────────────────────────────────────────────────────


async def test_an_answer_is_streamed_with_server_built_citations_and_stored_as_streamed(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    provider = rag.use(fake(*ANSWER_TOKENS, prompt=900, completion=120))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names[0] == "meta" and result.names[-2:] == ["citations", "done"]
    assert set(result.names[1:-2]) == {"token"}
    assert tokens_of(result) == ANSWER_TEXT
    items = result.first("citations")["items"]
    assert [set(i) for i in items] == [STUDENT_CITATION_KEYS] * 2
    assert items[0] == {"label": "S1", "document_title": "Intro to React", "location_kind": "page", "page_start": 3,
                        "page_end": 3, "heading_path": "Components > Props", "snippet": PROPS_TEXT}
    assert items[1]["label"] == "S2" and items[1]["page_start"] == 4 and items[1]["page_end"] == 5
    done = result.first("done")
    assert done["outcome"] == "answered" and done["partial"] is False
    assert done["usage"] == {"prompt_tokens": 900, "completion_tokens": 120, "estimated": False}
    assert len(provider.requests) == 1

    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content, answer.partial) == ("answered", ANSWER_TEXT, False)
    assert (answer.provider, answer.model_key, answer.prompt_version) == ("groq", ANSWER_MODEL, "rag-v1")
    assert (answer.prompt_tokens, answer.completion_tokens, answer.tokens_estimated) == (900, 120, False)
    assert answer.invalid_citation_count == 0 and answer.flagged_sources_used == 0 and answer.needs_review is False
    assert answer.retrieval is not None
    assert answer.retrieval["chunk_ids"] == [str(c.id) for c in material.chunks]
    assert answer.retrieval["reason"] == "similarity" and answer.retrieval["threshold"] == settings.RETRIEVAL_MIN_SIMILARITY
    assert SOURCE_CANARY not in json.dumps(answer.retrieval) and answer.retrieval_query is None
    assert answer.latency_ms is not None and {"embed", "retrieve", "first_token", "total"} <= set(answer.latency_ms)

    cites = await citation_rows(raw_db, answer.id)
    assert [(c.label, c.ordinal, c.chunk_id, c.document_id, c.version_id) for c in cites] == [
        ("S1", 1, material.chunks[0].id, material.document.id, material.version.id),
        ("S2", 2, material.chunks[1].id, material.document.id, material.version.id),
    ]
    assert cites[0].snippet == PROPS_TEXT and cites[1].snippet == STATE_TEXT and cites[0].user_id == w.s1.id
    assert cites[0].file_kind == "pdf" and cites[0].flagged is False and cites[0].similarity == pytest.approx(0.95)

    (usage,) = await usage_rows(raw_db, w.id)
    assert (usage.message_id, usage.user_id, usage.purpose, usage.outcome) == (answer.id, w.s1.id, "answer", "ok")
    assert (usage.provider, usage.model_key, usage.prompt_tokens, usage.completion_tokens, usage.estimated) == (
        "groq", ANSWER_MODEL, 900, 120, False)
    assert usage.attempt == 1
    # reserved before the call (estimate + max_output_tokens), settled to what the provider reported
    assert await tenant_tokens(w.id) == 900 + 120
    await assert_clean(w, w.s1.id)


async def test_the_request_carries_no_tools_and_the_sources_go_in_as_data(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    rec = rag.use_recording("120b_en_answerable")
    conv = await conversation(raw_db, w, w.s1)
    await ask(w, w.s1, conv.id)
    (body,) = rec.bodies
    assert not {"tools", "functions", "tool_choice", "response_format"} & set(body)
    system, *history, final = body["messages"]
    assert system["role"] == "system" and history == []
    assert final["role"] == "user" and SOURCE_CANARY in final["content"]
    assert final["content"].endswith(f"Student question:\n{QUESTION}")
    sent = json.dumps(body)
    for value in (material.document.id, material.version.id, *(c.id for c in material.chunks)):
        assert str(value) not in sent  # the model never sees an id
    assert "syllabus.pdf" not in sent and KEY_CANARY not in sent
    assert body["max_completion_tokens"] == settings.CHAT_MAX_OUTPUT_TOKENS and body["model"] == "openai/gpt-oss-120b"


async def test_an_invented_label_never_reaches_the_client(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    rag.use(fake("ANSWERABLE\n", "Props come from the parent [S7]", " and [S1]. ", "[S", "9]."))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert tokens_of(result) == "Props come from the parent  and [S1]. ."
    assert "S7" not in result.body.decode() and "S9" not in result.body.decode()
    assert [i["label"] for i in result.first("citations")["items"]] == ["S1"]
    _, answer = await rows(raw_db, conv.id)
    assert answer.content == tokens_of(result) and answer.invalid_citation_count == 2
    assert [c.label for c in await citation_rows(raw_db, answer.id)] == ["S1"]


async def test_no_valid_citation_is_retracted_to_the_fixed_refusal(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    rag.use(fake("ANSWERABLE\n", "Props are inputs.", " Nothing cited."))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names[0] == "meta" and result.names[-2:] == ["replace", "done"]
    assert result.first("replace") == {"text": REFUSAL_EN, "outcome": "no_valid_citations"}
    assert client_text(result) == REFUSAL_EN and "citations" not in result.names
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content) == ("no_valid_citations", REFUSAL_EN)
    assert await citation_rows(raw_db, answer.id) == []
    assert [u.outcome for u in await usage_rows(raw_db, w.id)] == ["ok"]


async def test_not_in_sources_closes_the_stream_at_once_and_settles_estimated_usage(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    leak = f"MODELTEXT{uuid.uuid4().hex[:8]}"
    provider = rag.use(fake("NOT", "_IN", "_S", "OURCES", "\n", leak, *(f" more {i}" for i in range(30)),
                            prompt=1500, completion=99))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names == ["meta", "replace", "done"]
    assert result.first("replace") == {"text": REFUSAL_EN, "outcome": "refused_by_model"}
    assert leak not in result.body.decode() and "NOT_IN" not in result.body.decode()
    assert provider.pulled == 5 and provider.closed == 1  # closed on the chunk that ended the line
    prompt_estimate = llm_budget.estimate_prompt_tokens(provider.requests[0])
    done = result.first("done")
    assert done["outcome"] == "refused_by_model"
    assert done["usage"] == {"prompt_tokens": prompt_estimate,
                             "completion_tokens": rag_answer.REFUSAL_COMPLETION_TOKENS_ESTIMATE, "estimated": True}
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content) == ("refused_by_model", REFUSAL_EN)
    assert (answer.prompt_tokens, answer.completion_tokens, answer.tokens_estimated) == (prompt_estimate, 128, True)
    (usage,) = await usage_rows(raw_db, w.id)
    assert (usage.prompt_tokens, usage.completion_tokens, usage.estimated) == (prompt_estimate, 128, True)
    assert await tenant_tokens(w.id) == prompt_estimate + 128
    assert await citation_rows(raw_db, answer.id) == []


async def test_finish_reason_length_is_stored_as_truncated(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    rag.use(fake("ANSWERABLE\n", "Props are inputs [S1] and", finish_reason="length"))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names[-2:] == ["citations", "done"] and result.first("done")["outcome"] == "truncated"
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content, answer.partial) == ("truncated", "Props are inputs [S1] and", False)


async def test_a_mid_stream_failure_is_stored_as_interrupted_with_the_partial_text(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    rag.use(scripted(Script(tokens=("ANSWERABLE\n", "Props are", " inputs [S1]", " and", " more"),
                            error=LLMUnavailable(provider="groq"), error_after_tokens=3)))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names[-1] == "error" and "done" not in result.names and "citations" not in result.names
    assert result.first("error") == {"code": "stream_interrupted", "partial": True,
                                     "message": chat_messages.STREAM_ERROR_MESSAGES["en"]["stream_interrupted"]}
    assert tokens_of(result) == "Props are inputs [S1]"
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.partial, answer.content) == ("interrupted", True, "Props are inputs [S1]")
    assert [c.label for c in await citation_rows(raw_db, answer.id)] == ["S1"]
    (usage,) = await usage_rows(raw_db, w.id)
    assert (usage.outcome, usage.estimated, usage.model_key) == ("interrupted", True, ANSWER_MODEL)
    assert 0 < await tenant_tokens(w.id) == usage.prompt_tokens + usage.completion_tokens
    await assert_clean(w, w.s1.id)


async def test_a_tool_call_response_ends_with_the_fixed_error_and_gives_the_budget_back(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    rag.use(scripted(Script.tool_call_response()))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names == ["meta", "error"]
    assert result.first("error")["code"] == "answer_unavailable" and result.first("error")["partial"] is False
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content) == ("error", "")
    (usage,) = await usage_rows(raw_db, w.id)
    assert (usage.outcome, usage.prompt_tokens, usage.completion_tokens) == ("bad_request", 0, 0)
    assert await tenant_tokens(w.id) == 0
    assert isinstance(Script.tool_call_response().error, LLMBadRequest)


# ── Budget and fallback (plan 7.5, 7.7) ───────────────────────────────────────


async def test_over_budget_gives_the_retrieval_only_answer_and_no_model_call(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    monkeypatch.setattr(settings, "LLM_TENANT_DAILY_TOKENS", 50)
    provider = rag.use(fake(*ANSWER_TOKENS))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    expected = f"{rag_answer.RETRIEVAL_ONLY_TEXTS['en']}\n\n[S1] [S2]"
    assert result.names == ["meta", "token", "citations", "done"] and tokens_of(result) == expected
    assert [i["label"] for i in result.first("citations")["items"]] == ["S1", "S2"]
    assert result.first("done")["outcome"] == "budget_retrieval_only"
    assert provider.requests == []
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content) == ("budget_retrieval_only", expected)
    assert len(await citation_rows(raw_db, answer.id)) == 2 and await usage_rows(raw_db, w.id) == []
    assert await tenant_tokens(w.id) == 0


async def test_every_model_failing_gives_the_fallback_answer_and_the_budget_back(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    provider = rag.use(scripted(Script(error=LLMRateLimited(retry_after_s=60, provider="groq"))))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.first("done")["outcome"] == "fallback_retrieval_only"
    assert tokens_of(result).startswith(rag_answer.RETRIEVAL_ONLY_TEXTS["en"])
    assert len(provider.requests) == 1
    _, answer = await rows(raw_db, conv.id)
    assert answer.outcome == "fallback_retrieval_only" and await usage_rows(raw_db, w.id) == []
    assert await tenant_tokens(w.id) == 0


async def test_with_the_fallback_off_every_model_failing_answers_answer_unavailable(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    monkeypatch.setattr(settings, "CHAT_FALLBACK_RETRIEVAL_ONLY", False)
    rag.use(scripted(Script(error=LLMRateLimited(retry_after_s=60, provider="groq"))))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names == ["meta", "error"] and result.first("error")["code"] == "answer_unavailable"
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content) == ("error", "")


async def test_an_embedding_failure_is_a_search_unavailable_event_without_a_model_call(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    provider = rag.use(fake(*ANSWER_TOKENS))
    rag.embed.fail = True
    conv = await conversation(raw_db, w, w.s1)
    with structlog.testing.capture_logs() as captured:
        result = await ask(w, w.s1, conv.id)
    assert result.names == ["meta", "error"] and result.first("error")["code"] == "search_unavailable"
    assert provider.requests == [] and "/secret/" not in str(captured)
    _, answer = await rows(raw_db, conv.id)
    assert answer.outcome == "error"


# ── T3 is one transaction ─────────────────────────────────────────────────────


async def test_t3_writes_the_row_the_citations_and_the_usage_in_one_transaction(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The usage insert comes after the citation inserts; when it fails, none of T3 stays (no citation row), the
    client gets internal_error and the closing step still gives the row its final state."""
    w = academic_world.a
    rag.use(fake(*ANSWER_TOKENS))

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("usage insert failed")

    monkeypatch.setattr(llm_usage, "record_usage", broken)
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names[-1] == "error" and result.first("error")["code"] == "internal_error"
    _, answer = await rows(raw_db, conv.id)
    assert answer.outcome == "error" and answer.content == ANSWER_TEXT
    assert await citation_rows(raw_db, answer.id) == [] and await usage_rows(raw_db, w.id) == []
    await assert_clean(w, w.s1.id)


# ── Scope, history, PII, flags ────────────────────────────────────────────────


async def test_nothing_outside_the_conversation_scope_reaches_the_model_or_the_answer(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    canaries = await seed_canaries(raw_db, academic_world)
    provider = rag.use(fake("ANSWERABLE\n", "Props [S1][S2][S3][S4][S5][S6][S7][S8]."))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    for text in canaries.texts.values():
        provider.inner.assert_never_sent(text)
        assert text not in result.body.decode()
    _, answer = await rows(raw_db, conv.id)
    assert answer.retrieval is not None
    assert set(answer.retrieval["chunk_ids"]) == {str(c.id) for c in material.chunks}
    cites = await citation_rows(raw_db, answer.id)
    assert {c.chunk_id for c in cites} <= {c.id for c in material.chunks} and not {c.chunk_id for c in cites} & canaries.chunk_ids


async def test_history_goes_into_the_prompt_with_old_labels_stripped_and_no_condense_call(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    provider = rag.use(fake(*ANSWER_TOKENS))
    conv = await conversation(raw_db, w, w.s1)
    await ask(w, w.s1, conv.id)
    await ask(w, w.s1, conv.id, "and what about state")
    assert len(provider.requests) == 2  # one model call per turn: LLM_CONDENSE_CHAIN is empty, no condense call
    second = provider.requests[1].messages
    assert [m.role for m in second] == ["user", "assistant", "user"]
    assert second[0].content == QUESTION and second[1].content == "A prop is an input and state is local ."
    assert second[2].content.endswith("Student question:\nand what about state")


async def test_the_model_gets_a_masked_question_and_the_stored_question_is_as_typed(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    provider = rag.use(fake(*ANSWER_TOKENS))
    conv = await conversation(raw_db, w, w.s1)
    first_name = w.s1.full_name.split()[0]
    question = f"what is a prop, says {first_name}, mail kid.{uuid.uuid4().hex[:6]}@example.com or 9876543210"
    with structlog.testing.capture_logs() as captured:
        await ask(w, w.s1, conv.id, question)
    final = provider.requests[0].messages[-1].content
    assert "[email]" in final and "[phone]" in final and "9876543210" not in final and "@example.com" not in final
    if len(first_name) >= 3:
        assert "[name]" in final
    question_row, _ = await rows(raw_db, conv.id)
    assert question_row.content == question
    assert "[email]" not in str(captured) and "9876543210" not in str(captured)


async def test_a_flagged_source_is_marked_and_an_answer_citing_only_it_needs_review(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    await seed_in_scope(raw_db, w, flags=("instruction_override",))
    provider = rag.use(fake("ANSWERABLE\n", "Props are inputs [S1]."))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert 'flagged="true"' in provider.requests[0].messages[-1].content
    assert set(result.first("citations")["items"][0]) == STUDENT_CITATION_KEYS  # no flag mark for a student
    _, answer = await rows(raw_db, conv.id)
    assert answer.flagged_sources_used == 1 and answer.needs_review is True
    assert (await citation_rows(raw_db, answer.id))[0].flagged is True


async def test_with_flagged_sources_excluded_and_only_flagged_hits_the_answer_is_the_refusal(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    monkeypatch.setattr(settings, "CHAT_FLAGGED_SOURCES", "exclude")
    await seed_in_scope(raw_db, w, flags=("instruction_override",), second_flags=("instruction_override",))
    provider = rag.use(fake(*ANSWER_TOKENS))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert tokens_of(result) == REFUSAL_EN and provider.requests == []
    _, answer = await rows(raw_db, conv.id)
    assert answer.outcome == "not_found" and answer.retrieval is not None
    assert answer.retrieval["reason"] == "flagged_excluded"


@pytest.mark.parametrize("role", ["s1", "t1", "admin"])
async def test_every_role_gets_the_student_citation_fields_only(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, role: str
) -> None:
    """The staff shape (flagged, document_id) is proposed in 8.2 and not built in this step: nobody gets ids,
    file names, scores or flag marks in the stream."""
    w = academic_world.a
    user = getattr(w, role)
    rag.use(fake(*ANSWER_TOKENS))
    conv = await conversation(raw_db, w, user)
    result = await ask(w, user, conv.id)
    items = result.first("citations")["items"]
    assert items and all(set(i) == STUDENT_CITATION_KEYS for i in items)
    body = result.body.decode()
    assert str(material.document.id) not in body and "syllabus.pdf" not in body and "0.95" not in body


# ── The real recordings (6a-6b) ───────────────────────────────────────────────

RECORDINGS = [
    ("120b_en_answerable", QUESTION), ("120b_mr_answerable", MR_QUESTION),
    ("20b_en_answerable", QUESTION), ("20b_mr_answerable", MR_QUESTION),
    ("120b_en_near_topic", QUESTION), ("120b_mr_near_topic", MR_QUESTION),
    ("20b_en_near_topic", QUESTION), ("20b_mr_near_topic", MR_QUESTION),
]


def expected_from_recording(name: str) -> tuple[str, str]:
    rec = read_recording(name)
    visible = "".join(rec.content)
    if name.endswith("near_topic"):
        return "refused_by_model", ""
    rest = visible.split("\n", 1)[1]
    return "answered", normalise_chunk(rest).lstrip(" \t\r\n")


@pytest.mark.parametrize(("name", "question"), RECORDINGS)
async def test_a_real_recording_through_the_wired_turn(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, name: str, question: str
) -> None:
    w = academic_world.a
    rec = read_recording(name)
    rag.use_recording(name)
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id, question)
    outcome, text = expected_from_recording(name)
    _, answer = await rows(raw_db, conv.id)
    assert answer.outcome == outcome and result.first("done")["outcome"] == outcome
    # the provider sent its usage before the stream ended, so it is the reported count (reasoning included)
    assert (answer.prompt_tokens, answer.completion_tokens, answer.tokens_estimated) == (*rec.usage, False)
    assert await tenant_tokens(w.id) == sum(rec.usage)
    if outcome == "answered":
        assert tokens_of(result) == text == answer.content
        assert "ANSWERABLE" not in result.body.decode()
        assert [i["label"] for i in result.first("citations")["items"]] == ["S1"]
        assert [c.label for c in await citation_rows(raw_db, answer.id)] == ["S1"]
    else:
        refusal = REFUSAL_MR if "_mr_" in name else REFUSAL_EN
        assert result.names == ["meta", "replace", "done"]
        assert "token" not in result.names and "NOT_IN" not in result.body.decode()
        assert answer.content == refusal == client_text(result)


@pytest.mark.parametrize(("name", "question"), RECORDINGS)
async def test_a_recording_in_one_character_chunks_gives_the_same_result(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, name: str, question: str
) -> None:
    w = academic_world.a
    rec = read_recording(name)
    chars = tuple("".join(rec.content))
    rag.use(fake(*chars, usage=UsageEvent(prompt_tokens=rec.usage[0], completion_tokens=rec.usage[1], estimated=False)))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id, question)
    outcome, text = expected_from_recording(name)
    _, answer = await rows(raw_db, conv.id)
    assert answer.outcome == outcome
    if outcome == "answered":
        assert tokens_of(result) == text == answer.content
        assert [c.label for c in await citation_rows(raw_db, answer.id)] == ["S1"]
    else:
        assert "token" not in result.names and answer.content == client_text(result)


@pytest.mark.parametrize("pieces", [("【S1】",), ("【", "S", "1", "】")])
async def test_a_full_width_citation_is_accepted_as_a_valid_label(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, pieces: tuple[str, ...]
) -> None:
    """The real mr-03 answer (6a-6b): 【S1】 at the end, no space before it."""
    w = academic_world.a
    rag.use(fake("ANSWERABLE\n", "Prop म्हणजे पालक घटकाकडून बाल घटकाला पाठवलेली माहिती", *pieces))
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id, MR_QUESTION)
    _, answer = await rows(raw_db, conv.id)
    assert answer.outcome == "answered" and answer.content.endswith("माहिती[S1]")
    assert tokens_of(result) == answer.content
    assert [c.label for c in await citation_rows(raw_db, answer.id)] == ["S1"]


# ── Idempotency ───────────────────────────────────────────────────────────────


async def test_a_retry_with_the_same_client_request_id_never_makes_a_second_answer(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    w = academic_world.a
    provider = rag.use(fake(*ANSWER_TOKENS))
    gate = provider.gates.setdefault(0, Gate())
    conv = await conversation(raw_db, w, w.s1)
    request_id = uuid.uuid4()
    first = asyncio.ensure_future(ask(w, w.s1, conv.id, client_request_id=request_id))
    await asyncio.wait_for(gate.entered.wait(), 10)
    while_streaming = await ask(w, w.s1, conv.id, client_request_id=request_id)
    assert while_streaming.status == 409 and while_streaming.json()["detail"]["outcome"] == "pending"
    gate.release.set()
    done = await asyncio.wait_for(first, 20)
    after = await ask(w, w.s1, conv.id, client_request_id=request_id)
    assert after.status == 409
    detail = after.json()["detail"]
    assert detail["outcome"] == "answered" and detail["message_id"] == done.first("meta")["message_id"]
    assert while_streaming.json()["detail"]["message_id"] == detail["message_id"]
    stored = await rows(raw_db, conv.id)
    assert [m.role for m in stored] == ["user", "assistant"] and len(provider.requests) == 1
    assert len(await usage_rows(raw_db, w.id)) == 1


# ── Disconnects at every phase (plan 8.6) ─────────────────────────────────────

PHASES = ["embedding", "retrieval", "llm_wait", "verdict", "mid_stream", "t3"]


@pytest.mark.parametrize("phase", PHASES)
async def test_a_disconnect_at_each_phase_leaves_one_closed_row_no_slot_and_no_connection(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    w = academic_world.a
    provider = rag.use(fake(*ANSWER_TOKENS, prompt=900, completion=120))
    gate = Gate()
    if phase == "embedding":
        rag.embed.gate = gate
    elif phase == "retrieval":
        monkeypatch.setattr(retrieval, "search_for_chat", gate.wrap(retrieval.search_for_chat))
    elif phase == "llm_wait":
        provider.gates[0] = gate
    elif phase == "verdict":
        provider.gates[2] = gate  # "ANSWER", "ABLE" pulled, the first line still held back
    elif phase == "mid_stream":
        provider.gates[6] = gate  # "A prop is an input" and " [" already sent
    else:
        monkeypatch.setattr(chat_messages, "_finish_row", gate.wrap(chat_messages._finish_row))
    gone = asyncio.Event()

    def leave(index: int, _chunk: bytes) -> None:
        if index == 1:
            async def go() -> None:
                await asyncio.wait_for(gate.entered.wait(), 10)
                gone.set()

            asyncio.ensure_future(go())

    result = await ask(w, w.s1, conv_id := (await conversation(raw_db, w, w.s1)).id, disconnect=gone, on_chunk=leave)
    gate.release.set()
    stored = await rows(raw_db, conv_id)
    assert [m.role for m in stored] == ["user", "assistant"]
    answer = stored[1]
    assert str(answer.id) == result.first("meta")["message_id"]
    assert (answer.outcome, answer.partial) == ("cancelled", True)
    assert answer.content == tokens_of(result)  # what the client actually received
    if phase == "mid_stream":
        assert answer.content == "A prop is an input "  # the space before the held "[" was already released
    usage = await usage_rows(raw_db, w.id)
    if phase in ("embedding", "retrieval", "llm_wait"):
        assert usage == []
        assert await tenant_tokens(w.id) == 0  # nothing reserved, or the reservation given back
    elif phase == "t3":
        assert [(u.outcome, u.prompt_tokens, u.completion_tokens, u.estimated) for u in usage] == [("ok", 900, 120, False)]
        assert await tenant_tokens(w.id) == 1020
    else:
        (row,) = usage
        assert (row.outcome, row.estimated, row.message_id) == ("cancelled", True, answer.id)
        assert await tenant_tokens(w.id) == row.prompt_tokens + row.completion_tokens
        assert (answer.prompt_tokens, answer.completion_tokens, answer.tokens_estimated) == (
            row.prompt_tokens, row.completion_tokens, True)
        assert provider.closed == 1
    await assert_clean(w, w.s1.id)


async def test_is_disconnected_is_checked_between_tokens(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any
) -> None:
    """Under ASGI spec 2.4 Starlette does not listen for the disconnect (it waits for send() to fail, and a server
    may just drop the sends): only the route's own check between tokens stops the provider stream. Honest limit: the
    server cannot see which of its sends the server dropped, so the stored text may hold the token or two sent
    between the disconnect and the check (never more than was sent, never a held-back label)."""
    w = academic_world.a
    tokens = ("ANSWERABLE\n", "A prop", " is", " an", " input", " [S1]", *(f" word{i}" for i in range(40)))
    provider = rag.use(fake(*tokens))
    conv = await conversation(raw_db, w, w.s1)
    gone = asyncio.Event()

    def leave_after_first_token(_index: int, chunk: bytes) -> None:
        if chunk.startswith(b"event: token"):
            gone.set()

    result = await ask(w, w.s1, conv.id, disconnect=gone, on_chunk=leave_after_first_token, spec_version="2.4")
    assert result.names == ["meta", "token"] and tokens_of(result) == "A prop"
    assert provider.pulled < len(tokens) / 2 and provider.closed == 1
    _, answer = await rows(raw_db, conv.id)
    full = "".join(tokens[1:])
    assert answer.outcome == "cancelled" and answer.content.startswith("A prop") and full.startswith(answer.content)
    assert len(answer.content) < len(full) / 2
    assert [u.outcome for u in await usage_rows(raw_db, w.id)] == ["cancelled"]
    await assert_clean(w, w.s1.id)


async def test_the_stream_cap_while_the_model_is_silent_ends_with_stream_interrupted(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    # The cap (3 s) is below the model's first-token timeout (10 s): only the stream cap can end this wait.
    monkeypatch.setattr(settings, "CHAT_STREAM_MAX_SECONDS", 3.0)
    assert settings.LLM_TIMEOUT_FIRST_TOKEN > 3.0
    provider = rag.use(fake(*ANSWER_TOKENS))
    gate = provider.gates[0] = Gate()  # never released by the test: the cap must end it
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert gate.entered.is_set() and len(provider.requests) == 1  # the cap fired while the model was silent
    assert result.names == ["meta", "error"] and result.first("error")["code"] == "stream_interrupted"
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.partial, answer.content) == ("interrupted", True, "")
    assert await tenant_tokens(w.id) == 0
    await assert_clean(w, w.s1.id)


# ── Connections ───────────────────────────────────────────────────────────────


async def test_no_connection_is_checked_out_while_embedding_waiting_for_the_model_or_streaming(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    provider = rag.use(fake(*ANSWER_TOKENS))
    at_event: list[int] = []
    real_event = chat_sse.event

    def spy_event(name: str, data: Any) -> bytes:
        at_event.append(checked_out())
        return real_event(name, data)

    monkeypatch.setattr(chat_sse, "event", spy_event)
    peak = {"current": 0, "max": 0}

    def checkout(*_a: Any) -> None:
        peak["current"] += 1
        peak["max"] = max(peak["max"], peak["current"])

    def checkin(*_a: Any) -> None:
        peak["current"] -= 1

    event.listen(app_engine.sync_engine, "checkout", checkout)
    event.listen(app_engine.sync_engine, "checkin", checkin)
    conv = await conversation(raw_db, w, w.s1)
    try:
        result = await ask(w, w.s1, conv.id)
    finally:
        event.remove(app_engine.sync_engine, "checkout", checkout)
        event.remove(app_engine.sync_engine, "checkin", checkin)
    assert result.first("done")["outcome"] == "answered"
    assert rag.embed.checkouts == [0], rag.embed.checkouts
    assert provider.checkouts and set(provider.checkouts) == {0}, provider.checkouts
    assert at_event and set(at_event) == {0}, at_event
    assert peak["max"] == 1, f"{peak['max']} connections were checked out at once"
    assert checked_out() == 0


# ── Logs ──────────────────────────────────────────────────────────────────────


async def test_no_question_source_or_answer_text_reaches_any_log(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    q_canary = f"QCANARY{uuid.uuid4().hex[:10]}"
    a_canary = f"ACANARY{uuid.uuid4().hex[:10]}"
    question = f"what is a prop {q_canary}"
    caplog.set_level("DEBUG")
    monkeypatch.setattr(settings, "CHAT_MESSAGES_PER_USER_PER_MINUTE", 50)
    conv = await conversation(raw_db, w, w.s1)
    with structlog.testing.capture_logs() as captured:
        rag.use(fake("ANSWERABLE\n", f"Props {a_canary} [S1]."))
        await ask(w, w.s1, conv.id, question)
        rag.use(fake("NOT_IN_SOURCES\n", a_canary))
        await ask(w, w.s1, conv.id, question)
        rag.use(scripted(Script(tokens=("ANSWERABLE\n", a_canary, "[S1]", "x"), error=LLMUnavailable(provider="groq"),
                                error_after_tokens=3)))
        await ask(w, w.s1, conv.id, question)
        rag.use(fake("ANSWERABLE\n", a_canary))
        await ask(w, w.s1, conv.id, question)
        gone = asyncio.Event()
        gone.set()
        await ask(w, w.s1, conv.id, question, disconnect=gone)
        rag.embed.fail = True
        await ask(w, w.s1, conv.id, question)
        rag.embed.fail = False
        monkeypatch.setattr(settings, "LLM_TENANT_DAILY_TOKENS", 10)
        await ask(w, w.s1, conv.id, question)
    logged = str(captured) + caplog.text
    for canary in (q_canary, a_canary, SOURCE_CANARY, "Intro to React", "Components > Props", KEY_CANARY):
        assert canary not in logged, canary
    turns = [e for e in captured if e.get("event") == "chat_turn"]
    assert len(turns) == 7 and all("user_id" not in e for e in turns)
    rag_keys = {"provider", "model_key", "prompt_version", "hits", "flagged_hits", "candidate_count", "reason",
                "invalid_citation_count", "prompt_tokens", "completion_tokens", "attempts"}
    assert rag_keys <= set(turns[0])
    assert turns[0]["outcome"] == "answered" and turns[0]["model_key"] == ANSWER_MODEL


async def test_the_prompt_builder_gets_the_cleaned_question_and_retrieval_the_same_text(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrieval uses the cleaned question; the masked copy goes only into the prompt."""
    w = academic_world.a
    rag.use(fake(*ANSWER_TOKENS))
    seen: dict[str, Any] = {}
    real_build = chat_prompt.build_prompt

    def spy_build(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(chat_prompt, "build_prompt", spy_build)
    conv = await conversation(raw_db, w, w.s1)
    await ask(w, w.s1, conv.id, "  what   is a prop​  mail me@example.com ")
    cleaned = "what is a prop mail me@example.com"
    assert rag.embed.queries == [cleaned] and seen["question"] == cleaned
    assert seen["mask"](cleaned) == "what is a prop mail [email]"
