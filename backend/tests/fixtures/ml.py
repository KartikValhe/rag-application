"""
Synthetic embedding model for the offline tests of step 5c-1 (see tests/fixtures/README_ml.md).

Nothing here downloads anything. The tokenizer is a tiny word-level `tokenizer.json` built with the
`tokenizers` library, and the model is a tiny ONNX graph built with the dev-only `onnx` package:

    hidden = reverse_cumsum(embedding_table[input_ids] * attention_mask) + embedding_table[input_ids]

so that (a) every real position depends on the real tokens after it (position 0 summarises the whole text, which
makes CLS pooling meaningful), (b) padding never changes the values at real positions (batching can be compared
with single-item runs), and (c) padding positions are non-zero, so pooling without the mask gives a wrong answer. It has the same inputs and output name as the real XLM-R based models.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

DIM = 8
SPECIALS = ["<s>", "<pad>", "</s>", "<unk>"]  # ids 0..3, the same layout as XLM-R
WORDS = (
    # prefixes (a WhitespaceSplit pre-tokenizer keeps "query:" as one token)
    ["query:", "passage:"]
    # English
    + ("the cat sat on mat dog barked at moon physics force mass acceleration energy light wave "  # noqa: SIM905
       "plant cell water river mountain history war king school teacher student exam syllabus").split()
    # Hindi
    + "भारत की राजधानी नई दिल्ली है विज्ञान बल द्रव्यमान पानी नदी पहाड़ शिक्षक छात्र परीक्षा".split()  # noqa: SIM905
    # Marathi
    + "महाराष्ट्राची मुंबई आहे इतिहास शेतकरी पाऊस शाळा विद्यार्थी शिक्षिका पुस्तक".split()  # noqa: SIM905
)
VOCAB = {tok: i for i, tok in enumerate(SPECIALS + WORDS)}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_tokenizer(path: Path) -> None:
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    tok = Tokenizer(models.WordLevel(VOCAB, unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tok.post_processor = processors.TemplateProcessing(
        single="<s> $A </s>",
        special_tokens=[("<s>", VOCAB["<s>"]), ("</s>", VOCAB["</s>"])],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tok.save(str(path))


def build_onnx_model(path: Path, *, token_type_ids: bool = False, external_data: str | None = None) -> None:
    """Write the synthetic model. `external_data` is a file name: store the weights in that side file."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    rng = np.random.RandomState(7)
    table = rng.normal(size=(len(VOCAB), DIM)).astype(np.float32)
    inits = [
        numpy_helper.from_array(table, "table"),
        numpy_helper.from_array(np.array([2], dtype=np.int64), "axes2"),
        numpy_helper.from_array(np.array(1, dtype=np.int64), "axis1"),
    ]
    nodes = [
        helper.make_node("Gather", ["table", "input_ids"], ["emb"], axis=0),
        helper.make_node("Cast", ["attention_mask"], ["mask_f"], to=TensorProto.FLOAT),
        helper.make_node("Unsqueeze", ["mask_f", "axes2"], ["mask_3d"]),
        helper.make_node("Mul", ["emb", "mask_3d"], ["masked"]),
        helper.make_node("CumSum", ["masked", "axis1"], ["suffix_sum"], reverse=1),
        # + the token's own vector: padding positions are NOT zero, so pooling over the mask matters
        helper.make_node("Add", ["suffix_sum", "emb"], ["last_hidden_state"]),
    ]
    inputs = [
        helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"]),
        helper.make_tensor_value_info("attention_mask", TensorProto.INT64, ["batch", "seq"]),
    ]
    if token_type_ids:
        inputs.append(helper.make_tensor_value_info("token_type_ids", TensorProto.INT64, ["batch", "seq"]))
    outputs = [helper.make_tensor_value_info("last_hidden_state", TensorProto.FLOAT, ["batch", "seq", DIM])]
    graph = helper.make_graph(nodes, "synthetic_encoder", inputs, outputs, initializer=inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 14)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    if external_data:
        onnx.save_model(
            model, str(path), save_as_external_data=True, all_tensors_to_one_file=True,
            location=external_data, size_threshold=1024,  # only the table; ORT needs the tiny shape constants inline
        
        )
    else:
        onnx.save_model(model, str(path))


@dataclass
class SyntheticModel:
    root: Path  # the directory that holds `onnx/model.onnx` and `onnx/tokenizer.json`
    entry: dict[str, Any]  # a lock-file entry that matches the files exactly

    @property
    def model_path(self) -> Path:
        return self.root / "onnx" / "model.onnx"

    @property
    def tokenizer_path(self) -> Path:
        return self.root / "onnx" / "tokenizer.json"


def lock_entry_for(root: Path, *, pooling: str, embedder: str, status: str = "pinned",
                   extra_files: tuple[str, ...] = (), max_tokens: int = 512) -> dict[str, Any]:
    paths = ["onnx/model.onnx", "onnx/tokenizer.json", *extra_files]
    files = [
        {"path": p, "sha256": sha256_of(root / p), "size": (root / p).stat().st_size} for p in paths
    ]
    return {
        "repo": "test/synthetic",
        "revision": "0123456789abcdef0123456789abcdef01234567",
        "embedder": embedder,
        "model_file": "onnx/model.onnx",
        "tokenizer_file": "onnx/tokenizer.json",
        "files": files,
        "dim": DIM,
        "max_tokens": max_tokens,
        "pooling": pooling,
        "pad_token": "<pad>",
        "prefixes": (
            {"query": "query: ", "passage": "passage: "} if embedder == "OnnxE5Embedder"
            else {"query": "", "passage": ""}
        ),
        "license": "MIT",
        "status": status,
    }


def make_synthetic(root: Path, *, pooling: str = "mean", embedder: str = "OnnxE5Embedder",
                   token_type_ids: bool = False, external_data: str | None = None,
                   declare_external: bool = True, max_tokens: int = 512) -> SyntheticModel:
    build_tokenizer(root / "onnx" / "tokenizer.json")
    build_onnx_model(root / "onnx" / "model.onnx", token_type_ids=token_type_ids, external_data=external_data)
    extra = (f"onnx/{external_data}",) if external_data and declare_external else ()
    entry = lock_entry_for(root, pooling=pooling, embedder=embedder, extra_files=extra, max_tokens=max_tokens)
    return SyntheticModel(root=root, entry=entry)


@pytest.fixture(scope="session")
def synthetic_e5_template(tmp_path_factory: pytest.TempPathFactory) -> SyntheticModel:
    return make_synthetic(tmp_path_factory.mktemp("synthetic_e5"))


@pytest.fixture
def synthetic_e5(synthetic_e5_template: SyntheticModel, tmp_path: Path) -> SyntheticModel:
    """A fresh copy per test (tests corrupt files), with a mean-pooling, e5-style lock entry."""
    root = tmp_path / "e5"
    shutil.copytree(synthetic_e5_template.root, root)
    return SyntheticModel(root=root, entry=json.loads(json.dumps(synthetic_e5_template.entry)))


@pytest.fixture(scope="session")
def synthetic_bge_template(tmp_path_factory: pytest.TempPathFactory) -> SyntheticModel:
    return make_synthetic(tmp_path_factory.mktemp("synthetic_bge"), pooling="cls", embedder="OnnxBgeM3Embedder")


@pytest.fixture
def synthetic_bge(synthetic_bge_template: SyntheticModel, tmp_path: Path) -> SyntheticModel:
    root = tmp_path / "bge"
    shutil.copytree(synthetic_bge_template.root, root)
    return SyntheticModel(root=root, entry=json.loads(json.dumps(synthetic_bge_template.entry)))
