"""
Step 6a-5 (docs/phase6-plan.md 9.1): app/services/llm_usage.record_usage writes one llm_usage_events row per LLM call.

Numbers, names and codes only: no content of any kind. The function has no parameter that could carry text, and the
two name fields (provider, model_key) must look like a provider name and a model key, so a question or an answer
cannot be smuggled into them. A refused row writes nothing and the error never repeats the value.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db.models.llm_usage import USAGE_OUTCOMES, USAGE_PURPOSES
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services import llm_usage
from app.services.llm_usage import UsageRecordError, record_usage

CANARY = "CANARY-question-7f3a what is the answer to the exam"


def _args(tid: uuid.UUID, /, **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "tenant_id": tid,
        "user_id": None,
        "message_id": None,
        "purpose": "answer",
        "provider": "groq",
        "model_key": "groq:openai/gpt-oss-120b",
        "prompt_tokens": 1200,
        "completion_tokens": 310,
        "estimated": False,
        "latency_ms": 2400,
        "outcome": "ok",
        "attempt": 1,
    }
    args.update(over)
    return args


async def _rows(raw_db: AsyncSession, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    res = await raw_db.execute(
        text("SELECT * FROM app.llm_usage_events WHERE tenant_id = :t ORDER BY created_at, id"), {"t": tenant_id}
    )
    return [dict(r) for r in res.mappings().all()]


def test_record_usage_takes_numbers_names_and_codes_only() -> None:
    """No parameter for a question, an answer, a prompt or any other text (plan 9.1)."""
    params = inspect.signature(record_usage).parameters
    assert list(params) == [
        "session", "tenant_id", "user_id", "message_id", "purpose", "provider", "model_key", "prompt_tokens",
        "completion_tokens", "estimated", "latency_ms", "outcome", "attempt",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for name, p in params.items() if name != "session")


async def test_one_row_with_exactly_the_given_numbers(tenant_a: Tenant, user_a: User, raw_db: AsyncSession) -> None:
    message = uuid.uuid4()  # a plain reference: no FK, it survives the conversation (plan 9.1)
    async with tenant_session_context(str(tenant_a.id)) as session:
        row_id = await record_usage(session, **_args(tenant_a.id, user_id=user_a.id, message_id=message))
    rows = await _rows(raw_db, tenant_a.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == row_id
    created = row.pop("created_at")
    assert created is not None
    assert row == {
        "tenant_id": tenant_a.id, "id": row_id, "user_id": user_a.id, "message_id": message, "purpose": "answer",
        "provider": "groq", "model_key": "groq:openai/gpt-oss-120b", "prompt_tokens": 1200, "completion_tokens": 310,
        "estimated": False, "latency_ms": 2400, "outcome": "ok", "attempt": 1,
    }


@pytest.mark.parametrize("purpose", USAGE_PURPOSES)
@pytest.mark.parametrize("outcome", USAGE_OUTCOMES)
async def test_every_purpose_and_outcome_is_stored(purpose: str, outcome: str, tenant_a: Tenant, raw_db: AsyncSession) -> None:
    async with tenant_session_context(str(tenant_a.id)) as session:
        await record_usage(
            session,
            **_args(tenant_a.id, purpose=purpose, outcome=outcome, estimated=True, prompt_tokens=0, completion_tokens=0, attempt=2),
        )
    [row] = await _rows(raw_db, tenant_a.id)
    assert (row["purpose"], row["outcome"], row["estimated"], row["attempt"]) == (purpose, outcome, True, 2)


@pytest.mark.parametrize(
    "over",
    [
        {"purpose": "chat"},
        {"outcome": "fine"},
        {"provider": "fake"},  # not on the allow-list
        {"provider": "openrouter"},  # does not match the model key's provider
        {"model_key": CANARY},
        {"model_key": f"groq:{CANARY}"},
        {"model_key": "groq:"},
        {"prompt_tokens": -1},
        {"completion_tokens": -1},
        {"latency_ms": -1},
        {"attempt": 0},
        {"prompt_tokens": True},  # a bool is not a count
        {"prompt_tokens": 1.5},
        {"estimated": 1},
        {"estimated": "no"},
        {"attempt": 2**15},
        {"prompt_tokens": 2**31},
        {"user_id": "not-a-uuid"},
        {"message_id": CANARY},
        {"tenant_id": str(uuid.uuid4())},
    ],
)
async def test_a_bad_value_is_refused_writes_nothing_and_is_not_repeated(
    over: dict[str, Any], tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    with pytest.raises(UsageRecordError) as caught:
        async with tenant_session_context(str(tenant_a.id)) as session:
            await record_usage(session, **_args(tenant_a.id, **over))
    message = str(caught.value)
    assert "CANARY" not in message and "exam" not in message
    for value in over.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in message, "the error repeats the refused value"
    assert await _rows(raw_db, tenant_a.id) == []


async def test_a_row_for_another_tenant_is_refused_by_rls(tenant_a: Tenant, tenant_b: Tenant, raw_db: AsyncSession) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session_context(str(tenant_a.id)) as session:
            await record_usage(session, **_args(tenant_b.id))
    assert await _rows(raw_db, tenant_b.id) == []


async def test_a_usage_row_cannot_be_changed_or_removed_by_the_tenant(tenant_a: Tenant, raw_db: AsyncSession) -> None:
    async with tenant_session_context(str(tenant_a.id)) as session:
        row_id = await record_usage(session, **_args(tenant_a.id))
    for statement in ("UPDATE app.llm_usage_events SET prompt_tokens = 0 WHERE id = :i", "DELETE FROM app.llm_usage_events WHERE id = :i"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_session_context(str(tenant_a.id)) as session:
                await session.execute(text(statement), {"i": row_id})
    [row] = await _rows(raw_db, tenant_a.id)
    assert row["prompt_tokens"] == 1200


def test_the_module_never_logs_or_formats_a_value() -> None:
    """The service has no logger: a refused row raises with the field name only (checked above)."""
    source = inspect.getsource(llm_usage)
    assert "logger" not in source and "print(" not in source
