"""
tests/fixtures/rag_world.py
───────────────────────────
Shared setup for the wired RAG turn (step 6a-10c, docs/phase6-plan.md 2.1, 4, 5, 6, 7).

  rag (fixture)        switches CHAT_RAG_ENABLED on, sets the answer chain to the decided model, and replaces the
                       query-embedder loader and the chain of app/services/rag_answer.py with the test's own; empties
                       the llm:* and usage:* Redis keys before and after
  EmbedSpy             the query embedder behind the loader (ScriptedEmbedder: every question gets the query axis,
                       so a chunk's similarity is what the test seeded); records queries and the checked-out
                       connections while it embeds; can be gated or made to fail
  ObservedProvider     wraps a provider client (FakeLLM, or the Groq adapter replaying a real recording): records
                       every request, the tokens it handed out, the checked-out connections at each token, whether
                       the stream was closed, and can hold before token N (a Gate)
  recording_client     the Groq adapter over httpx.MockTransport serving one of the 8 real recordings; records the
                       outgoing JSON bodies
  seed_in_scope / seed_canaries   the class material (10-A Math) and out-of-scope canaries at higher similarity

No test here reaches a network or needs a key: the key is a canary and every response is a recording or a script.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client
from app.core.config import settings
from app.db.engine import engine as app_engine
from app.db.models.chat import MessageCitation
from app.db.models.llm_usage import LLMUsageEvent
from app.llm.chain import LLMChain
from app.llm.fake import FakeLLM, Script
from app.llm.groq import GroqClient
from app.llm.models import load_registry
from app.llm.types import LLMEvent, LLMRequest, TokenEvent, UsageEvent
from app.services import rag_answer, retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import (
    ChunkSpec,
    ScriptedEmbedder,
    SeededDocument,
    axis_vector,
    seed_document,
)

ANSWER_MODEL = "groq:openai/gpt-oss-120b"
FIXTURES = Path(__file__).resolve().parent / "llm"
KEY_CANARY = "gsk-canary-6a10c-never-anywhere"
STUDENT_CITATION_KEYS = {"label", "document_title", "location_kind", "page_start", "page_end", "heading_path", "snippet"}

_real_get_redis = redis_client.get_redis


def checked_out() -> int:
    return int(app_engine.pool.checkedout())  # type: ignore[attr-defined]


class Gate:
    """Holds a coroutine of the turn until the test releases it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self) -> None:
        self.entered.set()
        await self.release.wait()

    def wrap(self, real: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def gated(*args: Any, **kwargs: Any) -> Any:
            await self.hold()
            return await real(*args, **kwargs)

        return gated


# ── The query embedder ────────────────────────────────────────────────────────


@dataclass
class EmbedSpy:
    embedder: Any = field(default_factory=lambda: ScriptedEmbedder({}))
    queries: list[str] = field(default_factory=list)
    checkouts: list[int] = field(default_factory=list)
    gate: Gate | None = None
    fail: bool = False

    async def load(self) -> retrieval.QueryEmbedder:
        inner = retrieval.QueryEmbedder.from_embedder(self.embedder)

        async def embed(query: str) -> list[float]:
            self.queries.append(query)
            self.checkouts.append(checked_out())
            if self.gate is not None:
                await self.gate.hold()
            if self.fail:
                raise RuntimeError("onnx exploded at /secret/models/e5")
            return await inner.embed(query)

        return retrieval.QueryEmbedder(model_key=inner.model_key, dim=inner.dim, embed=embed)


# ── The provider ──────────────────────────────────────────────────────────────


class ObservedProvider:
    """A provider client for the chain under the name "groq" (so usage rows name an allowed provider)."""

    provider = "groq"

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[LLMRequest] = []
        self.pulled = 0  # tokens handed to the chain
        self.closed = 0  # streams that ended (normally or closed early)
        self.checkouts: list[int] = []
        self.gates: dict[int, Gate] = {}  # token index -> hold before handing out that token

    async def stream(self, req: LLMRequest) -> AsyncIterator[LLMEvent]:
        self.requests.append(req)
        index = 0
        try:
            async for event in self.inner.stream(req):
                if isinstance(event, TokenEvent):
                    gate = self.gates.get(index)
                    if gate is not None:
                        await gate.hold()
                    self.checkouts.append(checked_out())
                    index += 1
                    self.pulled += 1
                yield event
        finally:
            self.closed += 1


def fake(*tokens: str, prompt: int = 900, completion: int = 120, **kw: Any) -> ObservedProvider:
    usage = kw.pop("usage", UsageEvent(prompt_tokens=prompt, completion_tokens=completion, estimated=False))
    return ObservedProvider(FakeLLM(default=Script(tokens=tokens, usage=usage, **kw)))


def scripted(script: Script) -> ObservedProvider:
    return ObservedProvider(FakeLLM(default=script))


def recording_path(name: str) -> Path:
    return FIXTURES / f"recorded_groq_openai_gpt-oss-{name}.sse"


@dataclass
class Recording:
    content: list[str]  # every non-empty delta.content, in order (the test's own reader, not the adapter)
    usage: tuple[int, int]


def read_recording(name: str) -> Recording:
    content: list[str] = []
    usage: tuple[int, int] | None = None
    for line in recording_path(name).read_text(encoding="utf-8").split("\n"):
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        value = json.loads(line[5:])
        for choice in value.get("choices", []):
            if choice.get("delta", {}).get("content"):
                content.append(choice["delta"]["content"])
        source = value.get("x_groq", {}).get("usage") or value.get("usage")
        if source:
            usage = (source["prompt_tokens"], source["completion_tokens"])
    assert usage is not None
    return Recording(content, usage)


@dataclass
class RecordingClient:
    client: GroqClient
    http: httpx.AsyncClient
    bodies: list[dict[str, Any]]


def recording_client(name: str) -> RecordingClient:
    raw = recording_path(name).read_bytes()
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RecordingClient(GroqClient(api_key=SecretStr(KEY_CANARY), http=http), http, bodies)


# ── The switch ────────────────────────────────────────────────────────────────


@dataclass
class RagSwitch:
    embed: EmbedSpy = field(default_factory=EmbedSpy)
    chain: LLMChain | None = None
    provider: ObservedProvider | None = None
    _http: list[httpx.AsyncClient] = field(default_factory=list)

    def use(self, provider: ObservedProvider) -> ObservedProvider:
        self.provider = provider
        self.chain = LLMChain(clients={"groq": provider}, registry=load_registry())
        return provider

    def use_recording(self, name: str) -> RecordingClient:
        rec = recording_client(name)
        self._http.append(rec.http)
        self.use(ObservedProvider(rec.client))
        return rec

    def get_chain(self) -> LLMChain:
        assert self.chain is not None, "the test did not choose a provider"
        return self.chain


async def _delete_keys(*patterns: str) -> None:
    r = await _real_get_redis()
    for pattern in patterns:
        keys = [key async for key in r.scan_iter(match=pattern, count=500)]
        if keys:
            await r.delete(*keys)


@pytest.fixture(name="rag")
async def rag_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[RagSwitch]:
    switch = RagSwitch()
    monkeypatch.setattr(settings, "CHAT_RAG_ENABLED", True)
    monkeypatch.setattr(settings, "LLM_ANSWER_CHAIN", ANSWER_MODEL)
    monkeypatch.setattr(settings, "LLM_CONDENSE_CHAIN", "")
    monkeypatch.setattr(rag_answer, "load_query_embedder", switch.embed.load)
    monkeypatch.setattr(rag_answer, "get_chain", switch.get_chain)
    await _delete_keys("llm:*", "usage:*")
    try:
        yield switch
    finally:
        for gate in ([switch.embed.gate] if switch.embed.gate else []) + list(
            switch.provider.gates.values() if switch.provider else []
        ):
            gate.release.set()
        for http in switch._http:
            await http.aclose()
        await _delete_keys("llm:*", "usage:*")


async def tenant_tokens(tenant_id: uuid.UUID) -> int:
    """Tokens counted today for the tenant (the daily budget counter of llm_budget)."""
    from app.services import llm_budget

    r = await _real_get_redis()
    value = await r.get(llm_budget.tenant_day_key(tenant_id, llm_budget.default_budget.today()))
    return int(value or 0)


# ── Class material ────────────────────────────────────────────────────────────

SOURCE_CANARY = f"SRCCANARY{uuid.uuid4().hex[:12]}"
PROPS_TEXT = f"A prop is an input that a parent component passes to a child component. {SOURCE_CANARY}"
STATE_TEXT = "State is data that one component owns and changes over time."


async def seed_in_scope(
    raw_db: AsyncSession, w: WorldTenant, *, sims: tuple[float, float] = (0.95, 0.90),
    flags: tuple[str, ...] = (), second_flags: tuple[str, ...] = (), title: str = "Intro to React",
) -> SeededDocument:
    """10-A Math, published: two chunks at the given similarities to every question (ingest flags per chunk)."""
    return await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.admin, title=title, file_kind="pdf",
        chunks=[
            ChunkSpec(PROPS_TEXT, heading="Components > Props", page_start=3, page_end=3,
                      vector=axis_vector(sims[0], 1), flags=flags),
            ChunkSpec(STATE_TEXT, heading="Components > State", page_start=4, page_end=5,
                      vector=axis_vector(sims[1], 2), flags=second_flags),
        ],
    )


@dataclass
class Canaries:
    texts: dict[str, str]
    chunk_ids: set[uuid.UUID]


async def seed_canaries(raw_db: AsyncSession, world: AcademicWorld) -> Canaries:
    """Chunks MORE similar than the class material, each outside the conversation's scope (10-A Math for S1):
    another subject of the same class, another class S1 is enrolled in, a class S1 is not in, another tenant, a
    draft (ready) version and an archived one."""
    a, b = world.a, world.b
    places: list[tuple[str, WorldTenant, str, tuple[str, ...], str]] = [
        ("subject", a, "Physics", ("10-A",), "published"),
        ("class_enrolled", a, "Chemistry", ("11-A",), "published"),
        ("class_other", a, "Math", ("10-B",), "published"),
        ("tenant", b, "Math", ("10-A",), "published"),
        ("draft", a, "Math", ("10-A",), "ready"),
        ("archived", a, "Math", ("10-A",), "archived"),
    ]
    texts: dict[str, str] = {}
    ids: set[uuid.UUID] = set()
    for axis, (name, wt, subject, classes, status) in enumerate(places, start=10):
        text = f"CANARY{name.upper()}{uuid.uuid4().hex[:10]} props are inputs of a component"
        doc = await seed_document(raw_db, wt, subject=subject, classes=classes, uploader=wt.admin, status=status,
                                  title=f"Canary {name}", chunks=[ChunkSpec(text, vector=axis_vector(0.99, axis))])
        texts[name] = text
        ids.update(c.id for c in doc.chunks)
    return Canaries(texts, ids)


async def citation_rows(raw_db: AsyncSession, message_id: uuid.UUID) -> list[MessageCitation]:
    await raw_db.commit()
    res = await raw_db.execute(select(MessageCitation).where(MessageCitation.message_id == message_id)
                               .order_by(MessageCitation.ordinal).execution_options(populate_existing=True))
    return list(res.scalars().all())


async def usage_rows(raw_db: AsyncSession, tenant_id: uuid.UUID) -> list[LLMUsageEvent]:
    await raw_db.commit()
    res = await raw_db.execute(select(LLMUsageEvent).where(LLMUsageEvent.tenant_id == tenant_id)
                               .order_by(LLMUsageEvent.created_at).execution_options(populate_existing=True))
    return list(res.scalars().all())
