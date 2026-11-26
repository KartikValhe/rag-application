"""
tests/fixtures/condense_world.py
────────────────────────────────
Shared setup for step 6a-11 (condense, tenant allowed_llm_providers narrowing, one usage row per failed attempt;
docs/phase6-plan.md 2.1 [D], 7.4, 7.5, 9.1, 12 6a-11).

  test_registry()      the shipped models.json plus test-only entries: a Groq condense model and an OpenRouter answer
                       and condense model (the shipped file has no condense model and no OpenRouter model yet)
  ByModel              one provider client that hands each request to the client of its model key (the chain maps
                       clients by provider name, so two models of one provider need this)
  use_models(...)      sets LLM_ANSWER_CHAIN / LLM_CONDENSE_CHAIN and gives the `rag` switch a chain over the test
                       registry; the jitter of the one retry is 0, so a retry never waits
  canary_client()      a real adapter over httpx.MockTransport that records every request it gets: a provider the
                       tenant does not allow must leave it empty
  set_allowed(...)     writes tenant_settings.allowed_llm_providers through the superuser (NULL, {} or a list)

No test reaches a network or needs a key: keys are canaries, responses are scripts or recordings.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.llm.chain import LLMChain
from app.llm.groq import GroqClient
from app.llm.models import ModelRegistry, RegisteredModel, load_registry
from app.llm.openrouter import OpenRouterClient
from app.llm.types import LLMEvent, LLMRequest
from tests.fixtures.rag_world import ANSWER_MODEL, RagSwitch, read_recording, recording_path

GROQ_CONDENSE = "groq:llama-3.1-8b-instant"
OR_ANSWER = "openrouter:test/answer-model"
OR_CONDENSE = "openrouter:test/condense-model"
CANARY_KEY = "sk-canary-6a11-never-anywhere"


def _model(key: str, purpose: str) -> RegisteredModel:
    provider, model_id = key.split(":", 1)
    return RegisteredModel(provider=provider, model_id=model_id, purposes=(purpose,), trains_on_inputs=False,
                           rpm=1000, rpd=100_000, pinned_on="2026-11-26")


def test_registry() -> ModelRegistry:
    shipped = load_registry().models
    return ModelRegistry(models=(*shipped, _model(GROQ_CONDENSE, "condense"), _model(OR_ANSWER, "answer"),
                                 _model(OR_CONDENSE, "condense")))


test_registry.__test__ = False  # type: ignore[attr-defined]  (a helper, not a test)


class ByModel:
    """A client for one provider name that passes each request to the client of its model key."""

    def __init__(self, by_key: Mapping[str, Any]) -> None:
        self.by_key = dict(by_key)

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        async for event in self.by_key[req.model_key].stream(req):
            yield event


def use_models(
    rag: RagSwitch,
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: Sequence[tuple[str, Any]],
    condense: Sequence[tuple[str, Any]] = (),
) -> LLMChain:
    """The two chains, in order, each model with its own client. Returns the chain the turn will use."""
    monkeypatch.setattr(settings, "LLM_ANSWER_CHAIN", ",".join(k for k, _ in answer))
    monkeypatch.setattr(settings, "LLM_CONDENSE_CHAIN", ",".join(k for k, _ in condense))
    per_provider: dict[str, dict[str, Any]] = {}
    for key, client in (*answer, *condense):
        per_provider.setdefault(key.split(":", 1)[0], {})[key] = client
    rag.chain = LLMChain(clients={p: ByModel(m) for p, m in per_provider.items()}, registry=test_registry(),
                         jitter=lambda: 0.0)
    for _, client in answer:
        if rag.provider is None and hasattr(client, "requests"):
            rag.provider = client  # so the fixture releases its gates
    return rag.chain


@dataclass
class Canary:
    """A real adapter whose transport records every request. `requests` must stay empty for a disallowed provider."""

    client: Any
    http: httpx.AsyncClient
    requests: list[httpx.Request] = field(default_factory=list)


def canary_client(provider: str) -> Canary:
    raw = recording_path("120b_en_answerable").read_bytes()
    canary = Canary(client=None, http=httpx.AsyncClient())

    def handler(request: httpx.Request) -> httpx.Response:
        canary.requests.append(request)
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    canary.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if provider == "groq":
        canary.client = GroqClient(api_key=SecretStr(CANARY_KEY), http=canary.http)
    else:
        canary.client = OpenRouterClient(api_key=SecretStr(CANARY_KEY), http=canary.http)
    return canary


async def set_allowed(raw_db: AsyncSession, tenant_id: uuid.UUID, providers: Sequence[str] | None) -> None:
    await raw_db.execute(
        text("INSERT INTO app.tenant_settings (tenant_id, allowed_llm_providers) VALUES (:t, :p) "
             "ON CONFLICT (tenant_id) DO UPDATE SET allowed_llm_providers = :p"),
        {"t": tenant_id, "p": None if providers is None else list(providers)},
    )
    await raw_db.commit()


async def delete_settings_row(raw_db: AsyncSession, tenant_id: uuid.UUID) -> None:
    await raw_db.execute(text("DELETE FROM app.tenant_settings WHERE tenant_id = :t"), {"t": tenant_id})
    await raw_db.commit()


async def settings_rows(raw_db: AsyncSession, tenant_id: uuid.UUID) -> int:
    await raw_db.commit()
    return int((await raw_db.execute(
        text("SELECT count(*) FROM app.tenant_settings WHERE tenant_id = :t"), {"t": tenant_id})).scalar_one())


__all__ = [
    "ANSWER_MODEL", "CANARY_KEY", "GROQ_CONDENSE", "OR_ANSWER", "OR_CONDENSE", "ByModel", "Canary", "canary_client",
    "delete_settings_row", "read_recording", "set_allowed", "settings_rows", "test_registry", "use_models",
]
