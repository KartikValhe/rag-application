"""
Step 5c-1: the model registry (`app/ml/models.lock.json`), the verifying loader and the lazy, once-per-process
instance for the API. Offline: tiny synthetic models only (tests/fixtures/ml.py), no socket (`block_sockets`).
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.ml import loader as loader_mod
from app.ml import model_keys
from app.ml.embedder import FakeEmbedder, OnnxBgeM3Embedder, OnnxE5Embedder
from app.ml.loader import load_embedder
from app.ml.lock import (
    DEFAULT_LOCK_PATH,
    ModelLoadError,
    ModelLockError,
    ModelVerificationError,
    hash_file,
    load_lock,
    verify_model_dir,
)
from tests.fixtures.ml import DIM, SyntheticModel, make_synthetic

pytestmark = pytest.mark.usefixtures("block_sockets")


def _write_lock(path: Path, name: str, entry: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"version": 1, "models": {name: entry}}), encoding="utf-8")
    return path


def _load(m: SyntheticModel, tmp_path: Path, **kw: Any) -> Any:
    name = m.root.name
    lock = _write_lock(tmp_path / "lock.json", name, m.entry)
    return load_embedder(name, m.root.parent, lock_path=lock, **kw)


# ── The real lock file ────────────────────────────────────────────────────────

# Values copied from docs/phase5-design.md section 0d (Hugging Face tree API, 2026-11-15). They are written
# out here on purpose: changing the lock file must be a visible, reviewed change of this test as well.
D0D_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
D0D_FILES = {
    "onnx/model.onnx": (470_268_510, "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"),
    "onnx/tokenizer.json": (17_082_730, "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"),
}


def test_the_real_lock_file_matches_design_0d_exactly() -> None:
    lock = load_lock()
    spec = lock.get("e5-small")
    assert spec.repo == "intfloat/multilingual-e5-small"
    assert spec.revision == D0D_REVISION
    assert {f.path: (f.size, f.sha256) for f in spec.files} == D0D_FILES
    assert (spec.dim, spec.max_tokens, spec.pooling, spec.license) == (384, 512, "mean", "MIT")
    assert spec.prefixes == {"query": "query: ", "passage": "passage: "}
    assert spec.embedder == "OnnxE5Embedder"
    assert spec.status == "pinned"
    assert (spec.model_file, spec.tokenizer_file) == ("onnx/model.onnx", "onnx/tokenizer.json")


def test_the_lock_file_agrees_with_the_shared_model_key_constants() -> None:
    spec = load_lock().get("e5-small")
    assert spec.model_key == model_keys.E5_SMALL_MODEL_KEY
    assert spec.repo == model_keys.E5_SMALL_REPO and spec.revision == model_keys.E5_SMALL_REVISION
    assert spec.dim == model_keys.E5_SMALL_DIM
    # a model can be named by its short name or by the stored key
    assert load_lock().get(model_keys.E5_SMALL_MODEL_KEY) == spec


def test_the_bge_m3_entry_is_unverified_and_cannot_be_loaded(tmp_path: Path) -> None:
    lock = load_lock()
    spec = lock.get("bge-m3")
    assert spec.status == "UNVERIFIED"
    assert spec.revision is None and spec.files == ()
    assert (spec.dim, spec.max_tokens, spec.pooling, spec.embedder) == (1024, 8192, "cls", "OnnxBgeM3Embedder")
    with pytest.raises(ModelVerificationError, match="UNVERIFIED"):
        load_embedder("bge-m3", tmp_path)


def test_every_pinned_entry_is_complete() -> None:
    for name, spec in load_lock().models.items():
        if spec.status == "UNVERIFIED":
            continue
        assert spec.revision and len(spec.revision) == 40, name
        assert spec.files and all(len(f.sha256) == 64 and f.size > 0 for f in spec.files), name


def test_the_lock_file_is_valid_json_with_only_known_keys() -> None:
    raw = json.loads(DEFAULT_LOCK_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"version", "models"}
    allowed = {"repo", "revision", "embedder", "model_file", "tokenizer_file", "files", "dim", "max_tokens",
               "pooling", "pad_token", "prefixes", "license", "status", "notes"}
    for entry in raw["models"].values():
        assert set(entry) <= allowed


# ── Lock validation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e["files"][0].update(sha256="abc"),  # not 64 hex
        lambda e: e["files"][0].update(sha256="G" * 64),
        lambda e: e["files"][0].update(sha256="A" * 64),  # upper case is refused: one spelling only
        lambda e: e["files"][0].update(size=0),
        lambda e: e["files"][0].update(size="12"),
        lambda e: e["files"][0].update(path="../escape.onnx"),
        lambda e: e["files"][0].update(path="/etc/passwd"),
        lambda e: e["files"][0].update(path="onnx\\model.onnx"),
        lambda e: e["files"][0].update(path="C:/x.onnx"),
        lambda e: e["files"].append(copy.deepcopy(e["files"][0])),  # duplicate path
        lambda e: e.update(model_file="onnx/missing.onnx"),
        lambda e: e.update(pooling="max"),
        lambda e: e.update(embedder="Evil"),
        lambda e: e.update(dim=0),
        lambda e: e.update(dim="8"),
        lambda e: e.update(max_tokens=0),
        lambda e: e.update(status="trusted"),
        lambda e: e.update(revision="main"),  # a branch name is not a pin
        lambda e: e.update(prefixes={"query": "", "passage": "passage: "}),  # does not match the e5 class
        lambda e: e.update(pooling="cls"),  # does not match the e5 class
        lambda e: e.update(unknown_key=1),
        lambda e: e.pop("files"),
    ],
)
def test_a_malformed_lock_entry_is_refused(synthetic_e5: SyntheticModel, tmp_path: Path, mutate: Any) -> None:
    entry = copy.deepcopy(synthetic_e5.entry)
    mutate(entry)
    lock = _write_lock(tmp_path / "lock.json", "e5", entry)
    with pytest.raises(ModelLockError):
        load_lock(lock)


def test_a_malformed_lock_document_is_refused(tmp_path: Path) -> None:
    for text in ("not json", "[]", '{"version": 2, "models": {}}', '{"version": 1}', '{"version": 1, "models": []}'):
        p = tmp_path / "lock.json"
        p.write_text(text, encoding="utf-8")
        with pytest.raises(ModelLockError):
            load_lock(p)


def test_an_unknown_model_name_is_refused() -> None:
    with pytest.raises(ModelLockError, match="unknown model"):
        load_lock().get("no-such-model")


# ── Loading ───────────────────────────────────────────────────────────────────


def test_a_verified_model_loads_and_embeds(synthetic_e5: SyntheticModel, tmp_path: Path) -> None:
    e = _load(synthetic_e5, tmp_path, batch_size=4, threads=1)
    assert isinstance(e, OnnxE5Embedder)
    assert e.dim == DIM and e.model_id == "test/synthetic@0123456789abcdef0123456789abcdef01234567"
    assert e.batch_size == 4
    assert len(e.embed_query("the cat")) == DIM


def test_the_loader_picks_the_class_named_in_the_lock(synthetic_bge: SyntheticModel, tmp_path: Path) -> None:
    e = _load(synthetic_bge, tmp_path)
    assert isinstance(e, OnnxBgeM3Embedder)


def test_the_loader_refuses_a_wrong_hash(synthetic_e5: SyntheticModel, tmp_path: Path) -> None:
    p = synthetic_e5.tokenizer_path
    data = bytearray(p.read_bytes())
    data[len(data) // 2] ^= 0x01  # same size, one flipped bit
    p.write_bytes(bytes(data))
    with pytest.raises(ModelVerificationError, match="sha256"):
        _load(synthetic_e5, tmp_path)


def test_the_loader_refuses_a_wrong_size_before_hashing(synthetic_e5: SyntheticModel, tmp_path: Path) -> None:
    with synthetic_e5.model_path.open("ab") as fh:
        fh.write(b"\0")
    with pytest.raises(ModelVerificationError, match="size"):
        _load(synthetic_e5, tmp_path)


def test_the_loader_refuses_a_missing_file(synthetic_e5: SyntheticModel, tmp_path: Path) -> None:
    synthetic_e5.tokenizer_path.unlink()
    with pytest.raises(ModelVerificationError, match="missing"):
        _load(synthetic_e5, tmp_path)


def test_the_loader_refuses_a_missing_model_directory(tmp_path: Path) -> None:
    entry = make_synthetic(tmp_path / "donor").entry
    lock = _write_lock(tmp_path / "lock.json", "absent", entry)
    with pytest.raises(ModelVerificationError, match="missing"):
        load_embedder("absent", tmp_path / "nowhere", lock_path=lock)


def test_the_loader_refuses_an_unverified_entry_even_when_the_files_are_perfect(
    synthetic_e5: SyntheticModel, tmp_path: Path
) -> None:
    synthetic_e5.entry["status"] = "UNVERIFIED"
    with pytest.raises(ModelVerificationError, match="UNVERIFIED"):
        _load(synthetic_e5, tmp_path)


@pytest.mark.parametrize("extra", ["pytorch_model.bin", "model.pkl", "onnx/custom_ops.dll", "onnx/extra.json"])
def test_the_loader_refuses_an_unexpected_extra_file(synthetic_e5: SyntheticModel, tmp_path: Path, extra: str) -> None:
    target = synthetic_e5.root / extra
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    with pytest.raises(ModelVerificationError, match="unexpected"):
        _load(synthetic_e5, tmp_path)


def test_the_loader_refuses_a_symlink(synthetic_e5: SyntheticModel, tmp_path: Path) -> None:
    link = synthetic_e5.root / "onnx" / "link.onnx"
    try:
        link.symlink_to(synthetic_e5.model_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created here")
    with pytest.raises(ModelVerificationError):
        _load(synthetic_e5, tmp_path)


def test_a_model_with_undeclared_external_data_is_refused(tmp_path: Path) -> None:
    m = make_synthetic(tmp_path / "ext", external_data="model.onnx_data", declare_external=False)
    with pytest.raises(ModelVerificationError, match="unexpected"):
        _load(m, tmp_path)


def test_a_model_whose_external_data_file_is_missing_is_refused_by_the_session(tmp_path: Path) -> None:
    m = make_synthetic(tmp_path / "ext2", external_data="model.onnx_data", declare_external=False)
    (m.root / "onnx" / "model.onnx_data").unlink()  # the model still points at it
    with pytest.raises(ModelLoadError):
        _load(m, tmp_path)


def test_external_data_declared_in_the_lock_with_its_hash_is_allowed_and_verified(tmp_path: Path) -> None:
    m = make_synthetic(tmp_path / "ext3", external_data="model.onnx_data", declare_external=True)
    paths = {f["path"] for f in m.entry["files"]}
    assert "onnx/model.onnx_data" in paths
    assert len(_load(m, tmp_path).embed_query("the cat")) == DIM
    side = m.root / "onnx" / "model.onnx_data"
    data = bytearray(side.read_bytes())
    data[0] ^= 0x01
    side.write_bytes(bytes(data))
    with pytest.raises(ModelVerificationError, match="sha256"):
        _load(m, tmp_path)


def test_the_loader_refuses_an_output_dimension_that_differs_from_the_lock(
    synthetic_e5: SyntheticModel, tmp_path: Path
) -> None:
    synthetic_e5.entry["dim"] = DIM + 1
    with pytest.raises(ModelLoadError, match="dim"):
        _load(synthetic_e5, tmp_path)


def test_verify_model_dir_and_hash_file_helpers(synthetic_e5: SyntheticModel, tmp_path: Path) -> None:
    lock = _write_lock(tmp_path / "lock.json", "e5", synthetic_e5.entry)
    spec = load_lock(lock).get("e5")
    verify_model_dir(spec, synthetic_e5.root)  # no exception
    size, digest = hash_file(synthetic_e5.model_path)
    assert size == synthetic_e5.model_path.stat().st_size and len(digest) == 64


def test_the_loader_never_opens_a_socket(synthetic_e5: SyntheticModel, tmp_path: Path, block_sockets: type) -> None:
    # the guard raises on any connect or DNS lookup; a clean load proves there was none
    _load(synthetic_e5, tmp_path).embed_passages(["the cat sat"])


# ── Settings ──────────────────────────────────────────────────────────────────


EMBEDDER_SETTING_NAMES = ("EMBEDDING_BATCH_SIZE", "EMBEDDING_THREADS", "EMBEDDING_MODEL_ID", "EMBEDDING_MODEL_DIR")


def _without_embedder_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the shell environment nor a .env file may change what these tests see."""
    for name in EMBEDDER_SETTING_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_settings_defaults_for_the_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_embedder_env(monkeypatch)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.EMBEDDING_BATCH_SIZE == 8  # 5c-2: measured best (design C3)
    assert s.EMBEDDING_THREADS == 1
    assert s.EMBEDDING_MODEL_ID == "e5-small"
    assert s.EMBEDDING_MODEL_DIR == ""


def test_settings_defaults_ignore_a_value_set_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # regression: a developer's EMBEDDING_MODEL_DIR must never leak into the default check above
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", "/somewhere/else")
    assert Settings(_env_file=None).EMBEDDING_MODEL_DIR == "/somewhere/else"  # type: ignore[call-arg]
    _without_embedder_env(monkeypatch)
    assert Settings(_env_file=None).EMBEDDING_MODEL_DIR == ""  # type: ignore[call-arg]


@pytest.mark.parametrize("field,value", [("EMBEDDING_BATCH_SIZE", 0), ("EMBEDDING_BATCH_SIZE", 1000),
                                          ("EMBEDDING_THREADS", 0)])
def test_settings_refuse_nonsense_embedder_values(field: str, value: int, monkeypatch: pytest.MonkeyPatch) -> None:
    _without_embedder_env(monkeypatch)
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})  # type: ignore[call-arg]


# ── Lazy, thread-safe, once-per-process instance ──────────────────────────────


@pytest.fixture
def fresh_singleton(monkeypatch: pytest.MonkeyPatch):
    loader_mod.reset_embedder_for_tests()
    yield
    loader_mod.reset_embedder_for_tests()


def test_get_embedder_builds_once_even_with_many_threads(
    fresh_singleton: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def slow_build() -> FakeEmbedder:
        calls.append(1)
        time.sleep(0.2)
        return FakeEmbedder(dim=8)

    monkeypatch.setattr(loader_mod, "_build_embedder", slow_build)
    got: list[Any] = []
    threads = [threading.Thread(target=lambda: got.append(loader_mod.get_embedder())) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1
    assert len({id(g) for g in got}) == 1 and len(got) == 12


def test_get_embedder_is_lazy_nothing_is_built_at_import_or_by_the_cheap_accessors(
    fresh_singleton: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise AssertionError("built too early")

    monkeypatch.setattr(loader_mod, "_build_embedder", boom)
    assert loader_mod.embedder_is_loaded() is False


def test_a_failed_build_is_not_cached_and_can_be_retried(fresh_singleton: None, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []

    def flaky() -> FakeEmbedder:
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelVerificationError("files missing")
        return FakeEmbedder(dim=8)

    monkeypatch.setattr(loader_mod, "_build_embedder", flaky)
    with pytest.raises(ModelVerificationError):
        loader_mod.get_embedder()
    assert loader_mod.embedder_is_loaded() is False
    assert isinstance(loader_mod.get_embedder(), FakeEmbedder)
    assert len(attempts) == 2


def test_build_embedder_reads_the_settings_and_refuses_when_no_directory_is_configured(
    fresh_singleton: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader_mod.settings, "EMBEDDING_MODEL_DIR", "")
    with pytest.raises(ModelLoadError, match="EMBEDDING_MODEL_DIR"):
        loader_mod.get_embedder()


def test_build_embedder_uses_the_configured_name_dir_batch_and_threads(
    fresh_singleton: None, synthetic_e5: SyntheticModel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = synthetic_e5.root.name
    lock = _write_lock(tmp_path / "lock.json", name, synthetic_e5.entry)
    monkeypatch.setattr(loader_mod, "DEFAULT_LOCK_PATH", lock)
    monkeypatch.setattr(loader_mod.settings, "EMBEDDING_MODEL_DIR", str(synthetic_e5.root.parent))
    monkeypatch.setattr(loader_mod.settings, "EMBEDDING_MODEL_ID", name)
    monkeypatch.setattr(loader_mod.settings, "EMBEDDING_BATCH_SIZE", 5)
    monkeypatch.setattr(loader_mod.settings, "EMBEDDING_THREADS", 2)
    e = loader_mod.get_embedder()
    assert isinstance(e, OnnxE5Embedder) and e.batch_size == 5
    assert e.session.get_session_options().intra_op_num_threads == 2
    assert loader_mod.get_embedder() is e


def test_the_worker_entry_point_still_refuses_to_start() -> None:
    """5c-1 builds the embedding layer only: the pipeline is wired in 5c-2/5c-3, so the entry point refuses."""
    import subprocess
    import sys

    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    res = subprocess.run([sys.executable, "-m", "app.workers.ingest"], capture_output=True, text=True,
                         env=env, timeout=60, cwd=str(Path(__file__).resolve().parents[1]))
    assert res.returncode == 2
    assert res.stderr.strip()
