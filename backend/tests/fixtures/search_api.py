"""
tests/fixtures/search_api.py
────────────────────────────
The query embedder of the test-search route (step 5d-2) for tests, and the helpers both test-search files
share (tests/test_test_search_route.py for the gate, tests/test_test_search_contract.py for the full tier).

The route gets a LOADER through the FastAPI dependency `get_query_embedder`; `query_embedder` overrides it with
a switch the test controls:

    switch.embedder        the embedder behind the loader (default: the lexical FakeEmbedder under the e5 key)
    switch.fail_load       the loader raises (the model cannot be loaded)
    switch.fail_embed      embedding the query raises
    switch.loads           how often the route asked for the embedder
    switch.queries         every query that reached the embedder
    switch.block           an asyncio.Event: while set to an unset event, every embedding waits on it (a
                           barrier, no sleeps)
    switch.on_embed        an async callback run inside every embedding (for example: revoke an assignment
                           between the scope and the search)
    switch.in_flight       embeddings running now; switch.max_in_flight is the most at any moment
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import documents as documents_api
from app.core.config import settings
from app.main import app
from app.services import retrieval
from tests.fixtures.academic_world import WorldTenant
from tests.fixtures.documents import auth
from tests.fixtures.retrieval_world import SeededDocument, fake_embedder, seed_document

URL = "/api/v1/documents/test-search"

RESPONSE_KEYS = {
    "sufficient", "reason", "message", "threshold", "threshold_calibrated", "keyword_coverage_threshold", "model",
    "candidate_count", "hits",
}
HIT_KEYS = {
    "rank", "chunk_id", "document_id", "document_title", "version_id", "version_no", "version_status", "page_start",
    "page_end", "heading_path", "content", "vector_rank", "vector_similarity", "keyword_rank", "keyword_matched_terms",
    "fused_score", "flagged", "injection_flags", "title_flagged", "filename_flagged",
}


class Pulse:
    """Wakes every waiter each time something it watches changes (the embedder's in_flight, the limiter's
    waiting count). Lets a test wait for a state instead of sleeping or counting event-loop turns."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def fire(self) -> None:
        self._event.set()
        self._event = asyncio.Event()

    async def wait(self) -> None:
        await self._event.wait()


HANG_GUARD_SECONDS = 60  # only stops a broken test from hanging; no test result depends on it


async def until(condition: Callable[[], bool], pulse: Pulse) -> None:
    """Wait until `condition` holds, re-checking it at every pulse. No clock decides the result: the guard only
    turns a hang into a failure."""
    while not condition():
        try:
            await asyncio.wait_for(pulse.wait(), timeout=HANG_GUARD_SECONDS)
        except TimeoutError:
            raise AssertionError("the condition never held (nothing changed for a minute)") from None


@dataclass
class EmbedderSwitch:
    embedder: Any = field(default_factory=fake_embedder)
    fail_load: bool = False
    fail_embed: bool = False
    loads: int = 0
    queries: list[str] = field(default_factory=list)
    block: asyncio.Event | None = None
    on_embed: Callable[[], Awaitable[None]] | None = None
    in_flight: int = 0
    max_in_flight: int = 0
    pulse: Pulse = field(default_factory=Pulse)

    async def load(self) -> retrieval.QueryEmbedder:
        self.loads += 1
        if self.fail_load:
            raise RuntimeError("model files missing at /secret/path")
        inner = retrieval.QueryEmbedder.from_embedder(self.embedder)

        async def embed(query: str) -> list[float]:
            self.queries.append(query)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.pulse.fire()
            try:
                if self.on_embed is not None:
                    await self.on_embed()
                if self.fail_embed:
                    raise RuntimeError("onnx exploded")
                if self.block is not None:
                    await self.block.wait()
                return await inner.embed(query)
            finally:
                self.in_flight -= 1
                self.pulse.fire()

        return retrieval.QueryEmbedder(model_key=inner.model_key, dim=inner.dim, embed=embed)


@pytest.fixture
def query_embedder() -> Iterator[EmbedderSwitch]:
    switch = EmbedderSwitch()
    app.dependency_overrides[documents_api.get_query_embedder] = lambda: switch.load
    try:
        yield switch
    finally:
        if switch.block is not None:
            switch.block.set()  # a failed test must not leave embeddings parked at the barrier
        app.dependency_overrides.pop(documents_api.get_query_embedder, None)


class ObservedLimiter(retrieval.EmbedLimiter):
    """The real limiter; only its `waiting` count also fires the switch's pulse when it changes."""

    def __init__(self, concurrency: int, queue_seconds: float, pulse: Pulse) -> None:
        self._pulse = pulse
        self._waiting = 0
        super().__init__(concurrency, queue_seconds)

    @property  # type: ignore[override]
    def waiting(self) -> int:
        return self._waiting

    @waiting.setter
    def waiting(self, value: int) -> None:
        self._waiting = value
        self._pulse.fire()


@pytest.fixture
def embed_limit(
    monkeypatch: pytest.MonkeyPatch, query_embedder: EmbedderSwitch
) -> Iterator[Callable[[int, float], retrieval.EmbedLimiter]]:
    """Set RETRIEVAL_EMBED_CONCURRENCY / RETRIEVAL_EMBED_QUEUE_SECONDS for one test: a fresh process-wide
    limiter (observed, so `until` can wait for its waiting count)."""

    def configure(concurrency: int, queue_seconds: float) -> retrieval.EmbedLimiter:
        monkeypatch.setattr(settings, "RETRIEVAL_EMBED_CONCURRENCY", concurrency)
        monkeypatch.setattr(settings, "RETRIEVAL_EMBED_QUEUE_SECONDS", queue_seconds)
        limiter = ObservedLimiter(concurrency, queue_seconds, query_embedder.pulse)
        monkeypatch.setattr(retrieval, "_embed_limiter", limiter)
        return limiter

    try:
        yield configure
    finally:
        retrieval.reset_embed_limiter()


def canary() -> str:
    return f"zq{uuid.uuid4().hex[:10]}"


async def doc(db: AsyncSession, w: WorldTenant, word: str, *, subject: str = "Math",
              classes: tuple[str, ...] = ("10-A",), status: str = "published", **kw: Any) -> SeededDocument:
    chunks = kw.pop("chunks", None) or [f"Lesson about {word} and waves."]
    return await seed_document(db, w, subject=subject, classes=classes, uploader=w.admin, embedder=fake_embedder(),
                               status=status, chunks=chunks, **kw)


async def search(client: AsyncClient, w: WorldTenant, user: Any, **body: Any) -> Response:
    return await client.post(URL, headers=auth(w, user), json=body)


def hit_ids(resp: Response) -> set[str]:
    return {h["chunk_id"] for h in resp.json()["hits"]}


def contents(resp: Response) -> str:
    return " ".join(str(h["content"]) for h in resp.json()["hits"])
