"""
tests/test_chat_narrowing.py
────────────────────────────
Tenant `allowed_llm_providers` in the RAG turn, and one usage row per failed attempt (step 6a-11; docs/phase6-plan.md
7.4, 7.5, 9.1; owner decisions 2, 3, 4 and 5 of the 6a-11 brief). Full app stack, real roles; providers are FakeLLMs or
real adapters over httpx.MockTransport ("canaries": every request they get is recorded). No network, no key.

Rules checked here:
  - NULL (or no tenant_settings row at all) is the platform chain; a missing row is read as the defaults and is NOT
    created by the read;
  - a list narrows the chain: a provider the tenant does not allow is never called (its canary transport stays empty);
  - nothing left (an empty list, only providers outside the chain, values that are not exact provider names): the
    existing retrieval-only answer (fallback_retrieval_only), no call, nothing reserved; with the fallback off the
    fixed answer_unavailable;
  - every provider call that failed leaves its own llm_usage_events row (outcome only, 0 tokens, estimated), next to
    the row of the call that answered; a skipped model (here: not allowed) leaves none.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.llm.errors import LLMBadRequest, LLMRateLimited, LLMUnavailable
from app.llm.fake import FakeLLM, Script
from app.llm.types import UsageEvent
from app.main import app
from app.services import rag_answer
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.chat_world import conversation, messages_path, raw_json, rows
from tests.fixtures.condense_world import (
    CANARY_KEY,
    OR_ANSWER,
    canary_client,
    delete_settings_row,
    set_allowed,
    settings_rows,
    use_models,
)
from tests.fixtures.documents import auth
from tests.fixtures.rag_world import (
    ANSWER_MODEL,
    ObservedProvider,
    RagSwitch,
    fake,
    rag_fixture,  # noqa: F401  (the `rag` pytest fixture)
    scripted,
    seed_in_scope,
    tenant_tokens,
    usage_rows,
)
from tests.fixtures.sse_client import SseResult, stream_request

pytestmark = pytest.mark.usefixtures("fixed_clock")

QUESTION = "what is a prop in react"
ANSWER_TOKENS = ("ANSWERABLE\n", "A prop is an input [S1] and state is local [S2].")
RETRIEVAL_ONLY = f"{rag_answer.RETRIEVAL_ONLY_TEXTS['en']}\n\n[S1] [S2]"


async def ask(w: WorldTenant, user: Any, conv_id: uuid.UUID, content: str = QUESTION) -> SseResult:
    return await stream_request(app, "POST", messages_path(conv_id), headers=auth(w, user), body=raw_json(content, None))


def tokens_of(result: SseResult) -> str:
    return "".join(e.data["text"] for e in result.events if e.name == "token")


@pytest.fixture
async def material(academic_world: AcademicWorld, raw_db: AsyncSession) -> Any:
    return await seed_in_scope(raw_db, academic_world.a)


def groq_canary(rag: RagSwitch) -> Any:
    canary = canary_client("groq")
    rag._http.append(canary.http)
    return canary


# ── NULL and the missing row ──────────────────────────────────────────────────


async def test_null_is_the_platform_chain(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    groq = fake(*ANSWER_TOKENS)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq), (OR_ANSWER, fake(*ANSWER_TOKENS))])
    await set_allowed(raw_db, w.id, None)
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.first("done")["outcome"] == "answered" and len(groq.requests) == 1


async def test_a_missing_settings_row_reads_as_the_defaults_and_is_not_created(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    groq = fake(*ANSWER_TOKENS)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq)])
    await delete_settings_row(raw_db, w.id)
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.first("done")["outcome"] == "answered" and len(groq.requests) == 1
    assert await settings_rows(raw_db, w.id) == 0


# ── Narrowing ─────────────────────────────────────────────────────────────────


async def test_a_provider_the_tenant_does_not_allow_is_never_called(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = groq_canary(rag)
    openrouter = fake(*ANSWER_TOKENS, prompt=700, completion=90)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, ObservedProvider(canary.client)), (OR_ANSWER, openrouter)])
    await set_allowed(raw_db, w.id, ["openrouter"])
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.first("done")["outcome"] == "answered"
    assert canary.requests == [] and len(openrouter.requests) == 1
    _, answer = await rows(raw_db, conv.id)
    assert (answer.provider, answer.model_key) == ("openrouter", OR_ANSWER)
    (usage,) = await usage_rows(raw_db, w.id)  # the skipped Groq model made no call, so it has no row
    assert (usage.provider, usage.model_key, usage.outcome) == ("openrouter", OR_ANSWER, "ok")
    assert CANARY_KEY not in result.body.decode()


async def test_another_tenant_is_not_narrowed_by_this_tenants_list(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = academic_world.a, academic_world.b
    await seed_in_scope(raw_db, b)
    groq = fake(*ANSWER_TOKENS)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq)])
    await set_allowed(raw_db, a.id, [])
    await set_allowed(raw_db, b.id, None)
    conv = await conversation(raw_db, b, b.s1)
    result = await ask(b, b.s1, conv.id)
    assert result.first("done")["outcome"] == "answered" and len(groq.requests) == 1


@pytest.mark.parametrize("allowed", [
    pytest.param([], id="empty_list"),
    pytest.param(["gemini"], id="only_outside_the_chain"),
    pytest.param(["GROQ", " groq", "groq ", "fake", ""], id="not_exact_names"),
])
async def test_nothing_left_gives_the_retrieval_only_answer_with_no_call_and_no_reservation(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any,
    monkeypatch: pytest.MonkeyPatch, allowed: list[str],
) -> None:
    w = academic_world.a
    canary = groq_canary(rag)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, ObservedProvider(canary.client))])
    reserved: list[int] = []
    real_reserve = rag_answer.llm_budget.TokenBudget.reserve

    async def spy_reserve(self: Any, tenant_id: uuid.UUID, amount: int, **kw: Any) -> Any:
        reserved.append(amount)
        return await real_reserve(self, tenant_id, amount, **kw)

    monkeypatch.setattr(rag_answer.llm_budget.TokenBudget, "reserve", spy_reserve)
    await set_allowed(raw_db, w.id, allowed)
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names == ["meta", "token", "citations", "done"]
    assert tokens_of(result) == RETRIEVAL_ONLY and result.first("done")["outcome"] == "fallback_retrieval_only"
    assert canary.requests == [] and reserved == []
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content, answer.provider) == ("fallback_retrieval_only", RETRIEVAL_ONLY, None)
    assert await usage_rows(raw_db, w.id) == [] and await tenant_tokens(w.id) == 0


async def test_nothing_left_with_the_fallback_off_is_answer_unavailable(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    canary = groq_canary(rag)
    monkeypatch.setattr(settings, "CHAT_FALLBACK_RETRIEVAL_ONLY", False)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, ObservedProvider(canary.client))])
    await set_allowed(raw_db, w.id, [])
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names == ["meta", "error"] and result.first("error")["code"] == "answer_unavailable"
    assert canary.requests == []
    _, answer = await rows(raw_db, conv.id)
    assert (answer.outcome, answer.content) == ("error", "")


async def test_the_list_is_read_on_every_turn(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    groq = fake(*ANSWER_TOKENS)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq)])
    conv = await conversation(raw_db, w, w.s1)
    await set_allowed(raw_db, w.id, ["groq"])
    assert (await ask(w, w.s1, conv.id)).first("done")["outcome"] == "answered"
    await set_allowed(raw_db, w.id, [])
    assert (await ask(w, w.s1, conv.id, "what is state in react")).first("done")["outcome"] == "fallback_retrieval_only"
    assert len(groq.requests) == 1


# ── One usage row per failed attempt ──────────────────────────────────────────


async def test_every_failed_attempt_has_its_own_usage_row_next_to_the_answering_one(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    groq = scripted(Script(error=LLMUnavailable(provider="groq")))
    openrouter = fake(*ANSWER_TOKENS, prompt=700, completion=90)
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq), (OR_ANSWER, openrouter)])
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.first("done")["outcome"] == "answered" and len(groq.requests) == 2  # one retry, then the fallback
    _, answer = await rows(raw_db, conv.id)
    usage = sorted(await usage_rows(raw_db, w.id), key=lambda u: (u.model_key != ANSWER_MODEL, u.attempt))
    assert [(u.model_key, u.outcome, u.attempt, u.prompt_tokens, u.completion_tokens, u.estimated) for u in usage] == [
        (ANSWER_MODEL, "unavailable", 1, 0, 0, True),
        (ANSWER_MODEL, "unavailable", 2, 0, 0, True),
        (OR_ANSWER, "ok", 1, 700, 90, False),
    ]
    assert all((u.message_id, u.user_id, u.purpose) == (answer.id, w.s1.id, "answer") for u in usage)
    assert (answer.model_key, answer.prompt_tokens) == (OR_ANSWER, 700)  # the row names the call that answered
    assert await tenant_tokens(w.id) == 790


async def test_every_model_failing_leaves_one_row_per_attempt_and_the_fallback_answer(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    groq = scripted(Script(error=LLMRateLimited(retry_after_s=60, provider="groq")))
    openrouter = scripted(Script(error=LLMUnavailable(provider="openrouter")))
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq), (OR_ANSWER, openrouter)])
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.first("done")["outcome"] == "fallback_retrieval_only"
    usage = await usage_rows(raw_db, w.id)
    assert sorted((u.model_key, u.outcome, u.attempt) for u in usage) == sorted([
        (ANSWER_MODEL, "rate_limited", 1), (OR_ANSWER, "unavailable", 1), (OR_ANSWER, "unavailable", 2)])
    _, answer = await rows(raw_db, conv.id)
    assert answer.provider is None and answer.prompt_tokens is None  # no call answered
    assert await tenant_tokens(w.id) == 0


async def test_a_refused_call_after_failed_attempts_is_counted_once(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    groq = scripted(Script(error=LLMUnavailable(provider="groq")))
    openrouter = scripted(Script(error=LLMBadRequest(provider="openrouter")))
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq), (OR_ANSWER, openrouter)])
    conv = await conversation(raw_db, w, w.s1)
    result = await ask(w, w.s1, conv.id)
    assert result.names == ["meta", "error"] and result.first("error")["code"] == "answer_unavailable"
    usage = await usage_rows(raw_db, w.id)
    assert sorted((u.model_key, u.outcome, u.attempt) for u in usage) == sorted([
        (ANSWER_MODEL, "unavailable", 1), (ANSWER_MODEL, "unavailable", 2), (OR_ANSWER, "bad_request", 1)])
    _, answer = await rows(raw_db, conv.id)
    assert answer.model_key == OR_ANSWER


async def test_a_usage_row_holds_no_text(
    rag: RagSwitch, academic_world: AcademicWorld, raw_db: AsyncSession, material: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    secret = f"PROVIDERTEXT{uuid.uuid4().hex[:8]}"
    groq = ObservedProvider(FakeLLM(default=Script(error=LLMUnavailable(provider="groq"))))
    openrouter = fake("ANSWERABLE\n", f"{secret} [S1].",
                      usage=UsageEvent(prompt_tokens=5, completion_tokens=5, estimated=False))
    use_models(rag, monkeypatch, answer=[(ANSWER_MODEL, groq), (OR_ANSWER, openrouter)])
    conv = await conversation(raw_db, w, w.s1)
    await ask(w, w.s1, conv.id, f"what is a prop {secret}")
    for row in await usage_rows(raw_db, w.id):
        values = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        assert secret not in str(values)
