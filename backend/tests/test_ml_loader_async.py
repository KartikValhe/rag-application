"""
Step 5c-1: the async accessors of the lazy embedder. Kept apart from tests/test_ml_loader.py because that module
blocks sockets, and the Windows event loop creates its own internal socket pair when the first async test runs.
The embedder here is a FakeEmbedder: nothing is loaded and nothing touches the network.
"""

import asyncio
import threading
import time

import pytest

from app.ml import loader as loader_mod
from app.ml.embedder import FakeEmbedder


@pytest.fixture
def fresh_singleton():
    loader_mod.reset_embedder_for_tests()
    yield
    loader_mod.reset_embedder_for_tests()


async def test_the_async_accessors_run_the_blocking_work_off_the_event_loop(
    fresh_singleton: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def build() -> FakeEmbedder:
        seen["build"] = threading.get_ident()
        return FakeEmbedder(dim=8)

    monkeypatch.setattr(loader_mod, "_build_embedder", build)
    vec = await loader_mod.embed_query_async("the cat")
    assert len(vec) == 8
    assert seen["build"] != loop_thread
    emb = await loader_mod.get_embedder_async()
    assert isinstance(emb, FakeEmbedder)
    # many concurrent first calls still build once
    loader_mod.reset_embedder_for_tests()
    seen.clear()
    count = 0

    def counted() -> FakeEmbedder:
        nonlocal count
        count += 1
        time.sleep(0.1)
        return FakeEmbedder(dim=8)

    monkeypatch.setattr(loader_mod, "_build_embedder", counted)
    await asyncio.gather(*[loader_mod.embed_query_async(f"q{i}") for i in range(6)])
    assert count == 1
