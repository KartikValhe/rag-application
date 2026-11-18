"""
The worker's entry point (step 5c-2, design A3 and C3): it starts ONLY when the embedding model files verify and
WORKER_DATABASE_URL, Redis and the document store are configured, loads the embedder ONCE, and has no switch that
selects the FakeEmbedder (tests inject it through the factory argument only).
"""

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.ml.embedder import FakeEmbedder
from app.workers import ingest, pipeline

APP = Path(__file__).resolve().parents[1] / "app"


class Overrides:
    """The real settings with a few values replaced (never reads or prints a secret)."""

    def __init__(self, **values: Any) -> None:
        self._values = values

    def __getattr__(self, name: str) -> Any:
        return self._values[name] if name in self._values else getattr(settings, name)


def never_loads(*_a: object, **_k: object) -> Any:
    raise AssertionError("the model must not be loaded when the configuration is incomplete")


GOOD = {
    "WORKER_DATABASE_URL": "postgresql+asyncpg://placeholder/placeholder",
    "REDIS_URL": "redis://localhost:6379/1",
    "DOCUMENT_STORE_DIR": "document_store",
    "EMBEDDING_MODEL_DIR": "models",
}


@pytest.mark.parametrize("name", ["WORKER_DATABASE_URL", "REDIS_URL", "DOCUMENT_STORE_DIR", "EMBEDDING_MODEL_DIR"])
def test_each_missing_requirement_is_named_and_nothing_is_loaded(name: str) -> None:
    cfg = Overrides(**{**GOOD, name: "  "})
    assert [p for p in pipeline.configuration_problems(cfg) if name in p]  # type: ignore[arg-type]
    with pytest.raises(ingest.WorkerNotConfigured, match=name):
        ingest.prepare_embedder(cfg, load=never_loads)  # type: ignore[arg-type]


def test_every_problem_is_listed_at_once() -> None:
    cfg = Overrides(WORKER_DATABASE_URL="", REDIS_URL="", DOCUMENT_STORE_DIR="", EMBEDDING_MODEL_DIR="")
    assert len(pipeline.configuration_problems(cfg)) == 4  # type: ignore[arg-type]


def test_model_files_that_do_not_verify_stop_the_start_with_a_clear_message(tmp_path: Path) -> None:
    cfg = Overrides(**{**GOOD, "EMBEDDING_MODEL_DIR": str(tmp_path)})  # an empty directory: nothing verifies
    with pytest.raises(ingest.WorkerNotConfigured) as exc:
        ingest.prepare_embedder(cfg)  # type: ignore[arg-type]
    assert "do not verify" in str(exc.value) and "fetch_model" in str(exc.value)


def test_an_unknown_model_name_is_refused_including_the_fake_models_id(tmp_path: Path) -> None:
    cfg = Overrides(**{**GOOD, "EMBEDDING_MODEL_DIR": str(tmp_path), "EMBEDDING_MODEL_ID": FakeEmbedder().model_id})
    with pytest.raises(ingest.WorkerNotConfigured):
        ingest.prepare_embedder(cfg)  # type: ignore[arg-type]


def test_the_embedder_is_loaded_once_with_the_configured_batch_size_and_threads_and_handed_to_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    handed: list[Any] = []

    def load(name: str, root: Path, *, batch_size: int, threads: int) -> FakeEmbedder:
        calls.append((name, root, batch_size, threads))
        return FakeEmbedder()

    async def amain(embedder: Any) -> None:
        handed.append(embedder)

    cfg = Overrides(**{**GOOD, "EMBEDDING_BATCH_SIZE": 5, "EMBEDDING_THREADS": 3, "EMBEDDING_MODEL_ID": "e5-small"})
    embedder = ingest.prepare_embedder(cfg, load=load)  # type: ignore[arg-type]
    asyncio.run(amain(embedder))
    assert calls == [("e5-small", Path("models"), 5, 3)]  # once
    assert handed == [embedder]


def test_main_exits_with_code_2_and_a_message_instead_of_starting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_MODEL_DIR", "")

    def boom(*_a: object) -> None:
        raise AssertionError("the worker must not start")

    monkeypatch.setattr(ingest, "_amain", boom)
    assert ingest.main() == 2
    err = capsys.readouterr().err
    assert "not started" in err and "EMBEDDING_MODEL_DIR" in err


def test_no_code_path_can_select_the_fake_embedder() -> None:
    """The fake is injectable only through the factory argument: nothing under app/ outside the embedder module
    builds one, and the worker's modules read no environment variable."""
    pattern = re.compile(r"FakeEmbedder\s*\(|import[^\n]*FakeEmbedder|from[^\n]*import[^\n]*FakeEmbedder")
    offenders = [
        str(p.relative_to(APP))
        for p in APP.rglob("*.py")
        if p.name != "embedder.py" and pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []
    for name in ("workers/ingest.py", "workers/pipeline.py", "ml/loader.py"):
        source = (APP / name).read_text(encoding="utf-8")
        assert "os.environ" not in source and "getenv" not in source, name
    lock = (APP / "ml" / "models.lock.json").read_text(encoding="utf-8").lower()
    assert "fake" not in lock  # and no lock entry names it
