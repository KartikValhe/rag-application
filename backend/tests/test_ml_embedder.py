"""
Step 5c-1: the embedding layer (design A1). Offline only: the FakeEmbedder and a tiny synthetic ONNX model
built in the test (tests/fixtures/ml.py). No test here downloads a model or opens a socket (`block_sockets`).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from app.ml import embedder as emb_mod
from app.ml.embedder import (
    Embedder,
    FakeEmbedder,
    ModelOutputError,
    OnnxBgeM3Embedder,
    OnnxE5Embedder,
    cls_pool,
    l2_normalize,
    mean_pool,
)
from tests.fixtures.ml import DIM, SyntheticModel

pytestmark = pytest.mark.usefixtures("block_sockets")

BACKEND = Path(__file__).resolve().parents[1]

EN = "The cat sat on the mat"
HI = "भारत की राजधानी नई दिल्ली है"
MR = "महाराष्ट्राची मुंबई आहे"


def _e5(m: SyntheticModel, **kw: Any) -> OnnxE5Embedder:
    return OnnxE5Embedder(
        m.model_path, m.tokenizer_path, model_id="test/synthetic@0", dim=DIM,
        max_tokens=kw.pop("max_tokens", 512), batch_size=kw.pop("batch_size", 16), **kw,
    )


def _bge(m: SyntheticModel, **kw: Any) -> OnnxBgeM3Embedder:
    return OnnxBgeM3Embedder(
        m.model_path, m.tokenizer_path, model_id="test/synthetic-bge@0", dim=DIM,
        max_tokens=kw.pop("max_tokens", 512), batch_size=kw.pop("batch_size", 16), **kw,
    )


def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (_norm(a) * _norm(b))


# ── Interface ────────────────────────────────────────────────────────────────


def test_all_embedders_satisfy_the_protocol(synthetic_e5: SyntheticModel, synthetic_bge: SyntheticModel) -> None:
    for e in (FakeEmbedder(dim=16), _e5(synthetic_e5), _bge(synthetic_bge)):
        assert isinstance(e, Embedder)
        assert isinstance(e.model_id, str) and e.model_id
        assert e.dim > 0 and e.max_tokens > 0
        assert isinstance(e.count_tokens("hello world"), int)


def test_output_is_plain_python_floats_of_the_right_size_and_unit_length(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5)
    for vec in (*e.embed_passages([EN, HI, MR]), e.embed_query(EN)):
        assert type(vec) is list and len(vec) == DIM
        assert all(type(x) is float for x in vec)
        assert abs(_norm(vec) - 1.0) < 1e-5


# ── Prefixes: recorded at the tokenizer (design A1 "Prefix test") ─────────────


class _Recorder:
    """Wraps `embedder._tok.encode_batch` and records the exact strings that reach the tokenizer."""

    def __init__(self, embedder: Any) -> None:
        self.calls: list[list[str]] = []
        original = embedder._tok.encode_batch

        def spy(texts: list[str]) -> Any:
            self.calls.append(list(texts))
            return original(texts)

        embedder._tok.encode_batch = spy


def test_query_prefix_is_added_exactly_once_in_embed_query(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5)
    rec = _Recorder(e)
    e.embed_query("What is force?")
    assert rec.calls == [["query: What is force?"]]


def test_passage_prefix_is_added_exactly_once_in_embed_passages_also_through_batching(
    synthetic_e5: SyntheticModel,
) -> None:
    e = _e5(synthetic_e5, batch_size=2)
    rec = _Recorder(e)
    e.embed_passages(["one", "two", "three", "four", "five"])
    assert rec.calls == [
        ["passage: one", "passage: two"],
        ["passage: three", "passage: four"],
        ["passage: five"],
    ]


def test_prefixes_are_never_swapped_or_doubled_even_when_the_text_already_looks_prefixed(
    synthetic_e5: SyntheticModel,
) -> None:
    e = _e5(synthetic_e5)
    rec = _Recorder(e)
    e.embed_query("passage: already")
    e.embed_passages(["query: already"])
    assert rec.calls == [["query: passage: already"], ["passage: query: already"]]
    for call in rec.calls:
        for s in call:
            assert not s.startswith("query: query: ") and not s.startswith("passage: passage: ")


def test_text_is_nfc_normalised_before_the_prefix_is_added(synthetic_e5: SyntheticModel) -> None:
    decomposed = "café"  # e + combining acute
    assert unicodedata.normalize("NFC", decomposed) != decomposed
    e = _e5(synthetic_e5)
    rec = _Recorder(e)
    e.embed_query(decomposed)
    e.embed_passages([decomposed])
    assert rec.calls == [["query: café"], ["passage: café"]]


def test_bge_m3_adds_no_prefix_at_all(synthetic_bge: SyntheticModel) -> None:
    e = _bge(synthetic_bge)
    rec = _Recorder(e)
    e.embed_query("What is force?")
    e.embed_passages(["one", "two"])
    assert rec.calls == [["What is force?"], ["one", "two"]]


def test_fake_embedder_records_the_prefixed_strings_it_received() -> None:
    e = FakeEmbedder(dim=32, batch_size=2)
    e.embed_query("What is force?")
    e.embed_passages(["a", "b", "c"])
    assert e.received_queries == ["query: What is force?"]
    assert e.received_passages == ["passage: a", "passage: b", "passage: c"]


def test_the_prefix_does_not_change_the_fake_vector_only_the_text_does() -> None:
    e = FakeEmbedder(dim=64)
    assert e.embed_query("force and mass") == e.embed_passages(["force and mass"])[0]


# ── Pooling and normalisation against a numpy reference ───────────────────────


def test_mean_pool_ignores_padding_positions() -> None:
    import numpy as np

    hidden = np.array([[[1.0, 1.0], [3.0, 3.0], [100.0, 100.0]]], dtype=np.float32)
    mask = np.array([[1, 1, 0]], dtype=np.int64)
    assert mean_pool(hidden, mask).tolist() == [[2.0, 2.0]]
    assert cls_pool(hidden).tolist() == [[1.0, 1.0]]


def test_l2_normalize_gives_unit_rows_and_leaves_a_zero_row_finite() -> None:
    import numpy as np

    out = l2_normalize(np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32))
    assert np.allclose(out[0], [0.6, 0.8])
    assert np.isfinite(out).all()


def _reference(model: SyntheticModel, texts: list[str], pooling: str, prefix: str) -> list[list[float]]:
    """An independent computation: one text at a time, no padding, plain numpy."""
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(model.tokenizer_path))
    sess = ort.InferenceSession(str(model.model_path), providers=["CPUExecutionProvider"])
    out = []
    for t in texts:
        ids = tok.encode(prefix + unicodedata.normalize("NFC", t)).ids
        hidden = sess.run(
            None,
            {"input_ids": np.array([ids], dtype=np.int64), "attention_mask": np.ones((1, len(ids)), dtype=np.int64)},
        )[0][0]
        v = hidden.mean(axis=0) if pooling == "mean" else hidden[0]
        out.append((v / np.linalg.norm(v)).astype(np.float32).tolist())
    return out


@pytest.mark.parametrize("text_set", [[EN], [HI], [MR], [EN, HI, MR, "dog barked at the moon"]])
def test_e5_mean_pooling_and_normalisation_match_the_numpy_reference(
    synthetic_e5: SyntheticModel, text_set: list[str]
) -> None:
    got = _e5(synthetic_e5, batch_size=8).embed_passages(text_set)
    ref = _reference(synthetic_e5, text_set, "mean", "passage: ")
    for g, r in zip(got, ref, strict=True):
        assert max(abs(a - b) for a, b in zip(g, r, strict=True)) < 1e-5


def test_bge_cls_pooling_and_normalisation_match_the_numpy_reference(synthetic_bge: SyntheticModel) -> None:
    texts = [EN, HI, MR]
    got = _bge(synthetic_bge).embed_passages(texts)
    ref = _reference(synthetic_bge, texts, "cls", "")
    for g, r in zip(got, ref, strict=True):
        assert max(abs(a - b) for a, b in zip(g, r, strict=True)) < 1e-5


def test_mean_and_cls_pooling_really_differ(synthetic_e5: SyntheticModel, synthetic_bge: SyntheticModel) -> None:
    # guards the reference test against a model where both poolings give the same vector
    assert _reference(synthetic_e5, [EN], "mean", "")[0] != _reference(synthetic_e5, [EN], "cls", "")[0]


# ── Batching, order, empty input ──────────────────────────────────────────────


TEXTS = [
    "cat",
    "the cat sat on the mat and the dog barked at the moon",
    HI,
    MR,
    "force mass acceleration",
    "water",
    "the river and the mountain and the history of the war and the king",
    "",
    EN,
    "student teacher exam",
]


def test_batching_equals_single_item_results(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5, batch_size=3)
    batched = e.embed_passages(TEXTS)
    singles = [e.embed_passages([t])[0] for t in TEXTS]
    for b, s in zip(batched, singles, strict=True):
        assert max(abs(x - y) for x, y in zip(b, s, strict=True)) < 1e-6


def test_the_number_of_model_runs_follows_the_batch_size(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5, batch_size=4)
    sizes: list[int] = []
    original = e.session.run

    def counting(out: Any, feed: dict[str, Any]) -> Any:
        sizes.append(len(feed["input_ids"]))
        return original(out, feed)

    e.session.run = counting
    e.embed_passages(TEXTS)  # 10 texts
    assert sizes == [4, 4, 2]


def test_order_is_preserved_and_deterministic(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5, batch_size=3)
    forward = e.embed_passages(TEXTS)
    backward = e.embed_passages(list(reversed(TEXTS)))
    assert forward == e.embed_passages(TEXTS)  # exactly equal, run to run
    for f, b in zip(forward, reversed(backward), strict=True):
        assert max(abs(x - y) for x, y in zip(f, b, strict=True)) < 1e-6
    assert forward[0] != forward[1]


def test_empty_input_returns_an_empty_list_and_runs_nothing(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5)
    rec = _Recorder(e)
    assert e.embed_passages([]) == []
    assert rec.calls == []
    assert FakeEmbedder(dim=8).embed_passages([]) == []


def test_a_non_string_input_is_a_type_error(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5)
    with pytest.raises(TypeError):
        e.embed_query(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        e.embed_passages(["ok", 3])  # type: ignore[list-item]


def test_a_bad_batch_size_or_thread_count_is_refused(synthetic_e5: SyntheticModel) -> None:
    with pytest.raises(ValueError):
        _e5(synthetic_e5, batch_size=0)
    with pytest.raises(ValueError):
        _e5(synthetic_e5, intra_op_threads=0)


# ── Truncation ────────────────────────────────────────────────────────────────


def test_long_text_is_truncated_never_an_exception_and_it_is_reported(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5, max_tokens=8)
    shapes: list[tuple[int, ...]] = []
    original = e.session.run

    def spy(out: Any, feed: dict[str, Any]) -> Any:
        shapes.append(tuple(feed["input_ids"].shape))
        return original(out, feed)

    e.session.run = spy
    long_text = " ".join(["cat"] * 100)
    assert e.truncated_count == 0
    vecs = e.embed_passages(["cat", long_text, "dog", long_text])
    assert len(vecs) == 4 and all(len(v) == DIM for v in vecs)
    assert e.truncated_count == 2  # only the two long texts
    assert all(s[1] <= 8 for s in shapes)  # special tokens and prefix included in the cap
    e.embed_query(long_text)
    assert e.truncated_count == 3


def test_text_that_fits_is_not_reported_as_truncated(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5, max_tokens=8)
    e.embed_passages(["cat sat", "dog"])
    assert e.truncated_count == 0


def test_count_tokens_counts_the_full_text_without_truncation_or_special_tokens(
    synthetic_e5: SyntheticModel,
) -> None:
    e = _e5(synthetic_e5, max_tokens=8)
    assert e.count_tokens("cat sat on mat") == 4
    assert e.count_tokens(" ".join(["cat"] * 100)) == 100  # not capped at max_tokens
    assert e.count_tokens("") == 0


def test_fake_embedder_truncates_and_reports_too() -> None:
    e = FakeEmbedder(dim=16, max_tokens=5)
    e.embed_passages(["a b", "one two three four five six seven"])
    assert e.truncated_count == 1


# ── ONNX runtime settings ─────────────────────────────────────────────────────


def test_session_uses_only_the_cpu_provider_and_the_configured_threads(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5, intra_op_threads=2)
    assert e.session.get_providers() == ["CPUExecutionProvider"]
    assert e.session.get_session_options().intra_op_num_threads == 2
    assert _e5(synthetic_e5).session.get_session_options().intra_op_num_threads == 1  # the default


def test_a_model_with_token_type_ids_is_fed_zeros_for_it(tmp_path: Path) -> None:
    from tests.fixtures.ml import make_synthetic

    m = make_synthetic(tmp_path / "tt", token_type_ids=True)
    e = _e5(m)
    assert len(e.embed_passages([EN])[0]) == DIM


def test_an_output_of_the_wrong_dimension_is_refused(synthetic_e5: SyntheticModel) -> None:
    e = OnnxE5Embedder(
        synthetic_e5.model_path, synthetic_e5.tokenizer_path, model_id="x@0", dim=DIM + 1, max_tokens=512
    )
    with pytest.raises(ModelOutputError):
        e.embed_query(EN)


def test_a_non_finite_output_is_refused(synthetic_e5: SyntheticModel, monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    e = _e5(synthetic_e5)
    real = e.session.run

    def nan_run(out: Any, feed: dict[str, Any]) -> Any:
        res = real(out, feed)
        res[0][:] = np.nan
        return res

    e.session.run = nan_run
    with pytest.raises(ModelOutputError):
        e.embed_query(EN)


def test_concurrent_calls_return_the_same_results_as_serial_ones(synthetic_e5: SyntheticModel) -> None:
    e = _e5(synthetic_e5, batch_size=2)
    expected = e.embed_passages(TEXTS)
    results: list[Any] = [None] * 6

    def work(i: int) -> None:
        results[i] = e.embed_passages(TEXTS)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r == expected for r in results)


# ── Hindi and Marathi ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["fake", "onnx_e5", "onnx_bge"])
def test_hindi_and_marathi_embed_without_error(
    name: str, synthetic_e5: SyntheticModel, synthetic_bge: SyntheticModel
) -> None:
    e: Any = {"fake": lambda: FakeEmbedder(dim=DIM), "onnx_e5": lambda: _e5(synthetic_e5),
              "onnx_bge": lambda: _bge(synthetic_bge)}[name]()
    texts = [HI, MR, "भारत की राजधानी", "शाळा विद्यार्थी", "विज्ञान की पुस्तक में जल चक्र का पाठ है।"]
    vecs = e.embed_passages(texts)
    assert len(vecs) == 5
    assert all(len(v) == DIM and abs(_norm(v) - 1) < 1e-5 for v in vecs)
    assert e.embed_passages([HI])[0] != e.embed_passages([MR])[0]
    e.embed_query(HI)


# ── FakeEmbedder behaviour ────────────────────────────────────────────────────


def test_fake_embedder_is_deterministic_and_unit_length_in_process() -> None:
    a, b = FakeEmbedder(dim=64), FakeEmbedder(dim=64)
    assert a.embed_passages([EN, HI]) == b.embed_passages([EN, HI])
    assert abs(_norm(a.embed_query(EN)) - 1) < 1e-9
    assert FakeEmbedder(dim=7).dim == 7 and len(FakeEmbedder(dim=7).embed_query(EN)) == 7


def test_fake_embedder_is_deterministic_across_processes_with_different_hash_seeds() -> None:
    code = (
        "import json\n"
        "from app.ml.embedder import FakeEmbedder\n"
        "e = FakeEmbedder(dim=48)\n"
        "print(json.dumps(e.embed_passages(['The cat sat on the mat', 'भारत की राजधानी नई दिल्ली है',"
        " 'force and mass'])))\n"
    )
    outs = []
    for seed in ("1", "12345", "random"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(BACKEND))
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                             cwd=str(BACKEND), timeout=60)
        assert res.returncode == 0, res.stderr
        outs.append(json.loads(res.stdout))
    assert outs[0] == outs[1] == outs[2]
    assert outs[0] == FakeEmbedder(dim=48).embed_passages(
        ["The cat sat on the mat", "भारत की राजधानी नई दिल्ली है", "force and mass"]
    )


def test_fake_embedder_gives_useful_lexical_similarity() -> None:
    e = FakeEmbedder(dim=384)
    q = e.embed_query("What is the force on a moving object")
    related = e.embed_passages(["The force on a moving object depends on its mass and acceleration"])[0]
    unrelated = e.embed_passages(["The river flows from the northern hills to the sea"])[0]
    assert _cos(q, related) > _cos(q, unrelated) + 0.2
    # also in Hindi
    qh = e.embed_query("भारत की राजधानी क्या है")
    near = e.embed_passages(["भारत की राजधानी नई दिल्ली है"])[0]
    far = e.embed_passages(["पौधे सूर्य के प्रकाश से अपना भोजन बनाते हैं"])[0]
    assert _cos(qh, near) > _cos(qh, far) + 0.2


def test_fake_embedder_handles_empty_and_punctuation_only_text() -> None:
    e = FakeEmbedder(dim=16)
    for t in ("", "   ", "!!! ???"):
        v = e.embed_passages([t])[0]
        assert abs(_norm(v) - 1) < 1e-9


# ── No network, no huggingface_hub, lazy imports ──────────────────────────────


def test_the_guard_really_blocks_sockets(block_sockets: type) -> None:
    import socket

    with pytest.raises(block_sockets):
        socket.create_connection(("127.0.0.1", 9))
    s = socket.socket()
    try:
        with pytest.raises(block_sockets):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()
    with pytest.raises(block_sockets):
        socket.getaddrinfo("huggingface.co", 443)


def _imports_after(code: str, extra_env: dict[str, str] | None = None) -> set[str]:
    full = (
        "import sys\n" + code + "\n"
        "bad = sorted({m.split('.')[0] for m in sys.modules} & "
        "{'onnxruntime','tokenizers','numpy','huggingface_hub','torch','transformers','onnx'})\n"
        "print('LOADED=' + ','.join(bad))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(BACKEND), **(extra_env or {}))
    res = subprocess.run([sys.executable, "-c", full], capture_output=True, text=True, env=env,
                         cwd=str(BACKEND), timeout=120)
    assert res.returncode == 0, res.stderr
    line = [ln for ln in res.stdout.splitlines() if ln.startswith("LOADED=")][-1]
    return set(filter(None, line.removeprefix("LOADED=").split(",")))


def test_importing_the_api_and_the_ml_modules_loads_no_ml_library() -> None:
    """Design A5: the API loads the model lazily. Importing the app, the embedder module, the loader and
    using the fake must not import onnxruntime, tokenizers or onnx. (numpy is not checked here: openpyxl, which
    the API imports, loads numpy itself whenever it is installed; the next test checks our own modules.)"""
    code = (
        "import app.main\n"
        "import app.ml.embedder, app.ml.loader, app.ml.lock\n"
        "from app.ml.embedder import FakeEmbedder\n"
        "FakeEmbedder(dim=8).embed_query('x')\n"
    )
    assert _imports_after(code) - {"numpy"} == set()


def test_our_ml_modules_alone_import_no_ml_library_not_even_numpy() -> None:
    code = (
        "import app.ml.embedder, app.ml.loader, app.ml.lock, app.ml.model_keys\n"
        "from app.ml.embedder import FakeEmbedder\n"
        "FakeEmbedder(dim=8).embed_passages(['x y z'])\n"
    )
    assert _imports_after(code) == set()


def test_huggingface_hub_torch_and_transformers_are_never_imported_even_when_a_model_runs(
    synthetic_e5: SyntheticModel, tmp_path: Path,
) -> None:
    name = synthetic_e5.root.name  # the model directory is <model root>/<lock key>
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"version": 1, "models": {name: synthetic_e5.entry}}), encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "from app.ml.loader import load_embedder\n"
        f"e = load_embedder({name!r}, Path(r'{synthetic_e5.root.parent}'), lock_path=Path(r'{lock}'))\n"
        "e.embed_query('the cat'); e.embed_passages(['the cat sat'])\n"
    )
    loaded = _imports_after(code, {"HF_HUB_OFFLINE": "0"})
    assert {"onnxruntime", "tokenizers", "numpy"} <= loaded  # the model really ran
    assert not loaded & {"huggingface_hub", "torch", "transformers"}


def _imported_top_level_names(path: Path) -> set[str]:
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_source_file_under_app_imports_huggingface_torch_or_transformers() -> None:
    banned = {"huggingface_hub", "torch", "transformers", "safetensors"}
    offenders = [p.name for p in (BACKEND / "app").rglob("*.py") if _imported_top_level_names(p) & banned]
    assert offenders == []


def test_the_ml_code_never_imports_a_deserialiser_that_can_run_code() -> None:
    banned = {"pickle", "cPickle", "dill", "joblib", "marshal", "shelve", "cloudpickle"}
    files = [*(BACKEND / "app" / "ml").glob("*.py")]
    files += [BACKEND / "app" / "scripts" / f"{n}.py" for n in ("fetch_model", "check_onnx_parity", "bench_embed")]
    assert all(f.exists() for f in files)
    offenders = [f.name for f in files if _imported_top_level_names(f) & banned]
    assert offenders == []


def test_the_ml_extra_pins_exact_versions_and_the_base_does_not_need_it() -> None:
    import tomllib

    data = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    ml = data["project"]["optional-dependencies"]["ml"]
    pinned = {r.split("==")[0].lower(): r.split("==")[1] for r in ml}
    assert set(pinned) == {"onnxruntime", "tokenizers", "numpy"}
    assert pinned["onnxruntime"] == "1.32.0" and pinned["tokenizers"] == "0.23.3"  # design 0d
    base = " ".join(data["project"]["dependencies"]).lower()
    assert "onnxruntime" not in base and "tokenizers" not in base
    forbidden = ("torch", "transformers", "huggingface")
    assert not any(f in " ".join(ml).lower() for f in forbidden)
    dev = " ".join(data["project"]["optional-dependencies"]["dev"]).lower()
    assert "rag-platform-backend[ml]" in dev and "onnx" in dev.replace("onnxruntime", "")


def test_module_has_the_expected_public_names() -> None:
    for name in ("Embedder", "FakeEmbedder", "OnnxE5Embedder", "OnnxBgeM3Embedder", "mean_pool", "cls_pool",
                 "l2_normalize"):
        assert hasattr(emb_mod, name)
