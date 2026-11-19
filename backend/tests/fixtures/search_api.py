"""
tests/fixtures/search_api.py
────────────────────────────
The query embedder of the test-search route (step 5d-2) for tests. The route gets a LOADER through the FastAPI
dependency `get_query_embedder`; `query_embedder` overrides it with a switch the test controls:

    switch.embedder      the embedder behind the loader (default: the lexical FakeEmbedder under the e5 key)
    switch.fail_load     the loader raises (the model cannot be loaded)
    switch.fail_embed    embedding the query raises
    switch.loads         how often the route asked for the embedder
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.api.v1 import documents as documents_api
from app.main import app
from app.services import retrieval
from tests.fixtures.retrieval_world import fake_embedder


@dataclass
class EmbedderSwitch:
    embedder: Any = field(default_factory=fake_embedder)
    fail_load: bool = False
    fail_embed: bool = False
    loads: int = 0
    queries: list[str] = field(default_factory=list)

    async def load(self) -> retrieval.QueryEmbedder:
        self.loads += 1
        if self.fail_load:
            raise RuntimeError("model files missing at /secret/path")
        inner = retrieval.QueryEmbedder.from_embedder(self.embedder)

        async def embed(query: str) -> list[float]:
            self.queries.append(query)
            if self.fail_embed:
                raise RuntimeError("onnx exploded")
            return await inner.embed(query)

        return retrieval.QueryEmbedder(model_key=inner.model_key, dim=inner.dim, embed=embed)


@pytest.fixture
def query_embedder() -> Iterator[EmbedderSwitch]:
    switch = EmbedderSwitch()
    app.dependency_overrides[documents_api.get_query_embedder] = lambda: switch.load
    try:
        yield switch
    finally:
        app.dependency_overrides.pop(documents_api.get_query_embedder, None)
