"""
The shared query embedder and the start-up warm-up flag (step 6a-1b, docs/phase6-plan.md section 12).

- app/services/query_embedding.py holds the guarded query embedder that test search used to keep inside the
  route: a load failure or an embedding failure becomes SearchUnavailable("search_unavailable"), a full
  embedding cap becomes SearchUnavailable("search_busy"), and only the exception class is ever logged.
- EMBEDDING_WARM_AT_STARTUP (off in development, on elsewhere unless set) loads the model once in the app
  lifespan, holds no database connection, and a load failure at start-up is logged without stopping the app.

No Postgres, Redis or child process: the loader's build step and the Redis calls of the lifespan are replaced.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from structlog.testing import capture_logs

from app import main as app_main
from app.api.v1 import documents as documents_api
from app.core.config import Settings, settings
from app.db.engine import engine as app_engine
from app.db.engine import platform_engine
from app.ml import loader
from app.services import query_embedding, retrieval
from tests.fixtures.retrieval_world import fake_embedder

pytestmark = pytest.mark.asyncio

SECRET = "/secret/path/to/model"


# ── The guarded embedder ─────────────────────────────────────────────────────


async def _loader_of(embed: Any, *, fail_load: bool = False) -> query_embedding.QueryEmbedderLoader:
    async def load() -> retrieval.QueryEmbedder:
        if fail_load:
            raise RuntimeError(f"model files missing at {SECRET}")
        return retrieval.QueryEmbedder(model_key="m", dim=3, embed=embed)

    return load


async def _ok_embed(query: str) -> list[float]:
    return [1.0, 0.0, 0.0]


async def test_a_loaded_embedder_is_returned_with_its_key_and_dimension_and_embeds() -> None:
    guarded = await query_embedding.guarded_query_embedder(await _loader_of(_ok_embed))
    assert (guarded.model_key, guarded.dim) == ("m", 3)
    assert await guarded.embed("anything") == [1.0, 0.0, 0.0]


async def test_a_load_failure_becomes_search_unavailable_and_logs_only_the_class() -> None:
    with capture_logs() as logs, pytest.raises(retrieval.SearchUnavailable) as raised:
        await query_embedding.guarded_query_embedder(await _loader_of(_ok_embed, fail_load=True))
    assert raised.value.code == "search_unavailable"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__  # no library text carried along
    assert SECRET not in repr(logs)
    errors = [e for e in logs if e["log_level"] == "error"]
    assert [(e["stage"], e["error"]) for e in errors] == [("load", "RuntimeError")]


async def test_an_embedding_failure_becomes_search_unavailable_and_logs_only_the_class() -> None:
    async def boom(query: str) -> list[float]:
        raise RuntimeError(f"onnx exploded on {query}")

    guarded = await query_embedding.guarded_query_embedder(await _loader_of(boom))
    with capture_logs() as logs, pytest.raises(retrieval.SearchUnavailable) as raised:
        await guarded.embed("canary-query")
    assert raised.value.code == "search_unavailable"
    assert "canary-query" not in repr(logs) and "onnx" not in repr(logs)
    assert [(e["stage"], e["error"]) for e in logs if e["log_level"] == "error"] == [("embed", "RuntimeError")]


async def test_the_log_event_name_can_be_chosen_by_the_caller() -> None:
    with capture_logs() as logs, pytest.raises(retrieval.SearchUnavailable):
        await query_embedding.guarded_query_embedder(
            await _loader_of(_ok_embed, fail_load=True), log_event="test_search_embedder_unavailable"
        )
    assert [e["event"] for e in logs] == ["test_search_embedder_unavailable"]


async def test_over_the_cap_embedding_is_search_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap itself stays in retrieval.embed_query; the guarded embedder is what it is applied to."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow(query: str) -> list[float]:
        started.set()
        await release.wait()
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(retrieval, "_embed_limiter", retrieval.EmbedLimiter(1, 0))
    guarded = await query_embedding.guarded_query_embedder(await _loader_of(slow))
    first = asyncio.create_task(retrieval.embed_query(guarded, "one"))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        with pytest.raises(retrieval.SearchUnavailable) as raised:
            await retrieval.embed_query(guarded, "two")
        assert raised.value.code == "search_busy"
    finally:
        release.set()
        await first


async def test_the_route_takes_its_loader_from_the_shared_module() -> None:
    assert documents_api.get_query_embedder() is query_embedding.process_query_embedder
    assert documents_api.QueryEmbedderLoader is query_embedding.QueryEmbedderLoader
    assert not hasattr(documents_api, "_guarded_query_embedder")
    assert not hasattr(documents_api, "_process_query_embedder")


# ── The setting ──────────────────────────────────────────────────────────────


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    monkeypatch.delenv("EMBEDDING_WARM_AT_STARTUP", raising=False)
    return Settings(_env_file=None, REDIS_PASSWORD="a-redis-password", **overrides)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("environment", "expected"),
    [("development", False), ("DEVELOPMENT", False), ("staging", True), ("production", True)],
)
async def test_the_warm_up_default_is_off_in_development_and_on_elsewhere(
    monkeypatch: pytest.MonkeyPatch, environment: str, expected: bool
) -> None:
    assert _settings(monkeypatch, ENVIRONMENT=environment).embedding_warm_at_startup is expected


@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize("value", [True, False])
async def test_an_explicit_warm_up_setting_wins_in_every_environment(
    monkeypatch: pytest.MonkeyPatch, environment: str, value: bool
) -> None:
    s = _settings(monkeypatch, ENVIRONMENT=environment, EMBEDDING_WARM_AT_STARTUP=value)
    assert s.embedding_warm_at_startup is value


async def test_the_warm_up_setting_is_documented_in_env_example() -> None:
    from pathlib import Path

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "EMBEDDING_WARM_AT_STARTUP" in example


# ── The warm-up in the lifespan ──────────────────────────────────────────────


class Build:
    """Replaces the loader's blocking build step: counts calls and looks at the connection pools meanwhile."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.checked_out: list[int] = []

    def __call__(self) -> Any:
        self.calls += 1
        self.checked_out.append(int(app_engine.pool.checkedout()) + int(platform_engine.pool.checkedout()))  # type: ignore[attr-defined]
        if self.fail:
            raise RuntimeError(f"model files missing at {SECRET}")
        return fake_embedder()


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch) -> Iterator[Build]:
    loader.reset_embedder_for_tests()
    b = Build()
    monkeypatch.setattr(loader, "_build_embedder", b)

    async def no_redis(*_a: object, **_k: object) -> None:  # the lifespan's Redis calls are not under test
        return None

    monkeypatch.setattr(app_main, "get_redis", no_redis)
    monkeypatch.setattr(app_main, "close_redis", no_redis)
    try:
        yield b
    finally:
        loader.reset_embedder_for_tests()


async def _run_lifespan() -> None:
    async with app_main.lifespan(app_main.app):
        pass


async def test_with_the_flag_on_the_lifespan_loads_the_embedder_once_and_holds_no_connection(
    monkeypatch: pytest.MonkeyPatch, build: Build
) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_WARM_AT_STARTUP", True)
    await _run_lifespan()
    assert build.calls == 1
    assert loader.embedder_is_loaded()
    assert build.checked_out == [0]  # no pooled connection while the model loaded
    assert app_engine.pool.checkedout() == 0 and platform_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    # ... and the cached instance serves the first search: no second load
    await loader.get_embedder_async()
    await query_embedding.process_query_embedder()
    assert build.calls == 1


async def test_with_the_flag_off_nothing_is_loaded(monkeypatch: pytest.MonkeyPatch, build: Build) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_WARM_AT_STARTUP", False)
    await _run_lifespan()
    assert build.calls == 0
    assert not loader.embedder_is_loaded()


async def test_the_development_default_loads_nothing_at_startup(monkeypatch: pytest.MonkeyPatch, build: Build) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_WARM_AT_STARTUP", None)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    await _run_lifespan()
    assert build.calls == 0


async def test_a_load_failure_at_startup_is_logged_and_does_not_stop_the_app(
    monkeypatch: pytest.MonkeyPatch, build: Build
) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_WARM_AT_STARTUP", True)
    build.fail = True
    with capture_logs() as logs:
        await _run_lifespan()  # must not raise
    assert build.calls == 1
    assert SECRET not in repr(logs)  # only the exception class is logged
    assert [e["error"] for e in logs if e["event"] == "embedder_warm_up_failed"] == ["RuntimeError"]
    assert not loader.embedder_is_loaded()
    # the failure is not cached: the first search retries the load
    build.fail = False
    await query_embedding.process_query_embedder()
    assert build.calls == 2 and loader.embedder_is_loaded()


async def test_warming_up_twice_loads_once(build: Build) -> None:
    assert await query_embedding.warm_up_embedder() is True
    assert await query_embedding.warm_up_embedder() is True
    assert build.calls == 1


async def test_warm_up_reports_a_failure_instead_of_raising(build: Build) -> None:
    build.fail = True
    assert await query_embedding.warm_up_embedder() is False
