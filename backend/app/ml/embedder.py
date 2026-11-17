"""
app/ml/embedder.py
──────────────────
The embedding layer (design A1, step 5c-1): the `Embedder` protocol and three implementations.

    OnnxE5Embedder     multilingual-e5-small through onnxruntime: mean pooling, `query: ` / `passage: ` prefixes
    OnnxBgeM3Embedder  bge-m3 (the documented upgrade path): CLS pooling, no prefix
    FakeEmbedder       deterministic feature hashing, for tests and CI (no model, no ML library)

Rules every implementation follows:
  * the model prefix is added INSIDE `embed_query` / `embed_passages`, exactly once, after NFC normalisation;
    callers pass plain text and never add a prefix themselves;
  * text longer than `max_tokens` is truncated by the tokenizer, never an exception; the number of truncated
    texts is counted in `truncated_count` so the chunker can be tuned;
  * `embed_passages` works in batches of `batch_size` and returns vectors in input order;
  * vectors are L2-normalised and returned as plain Python floats.

onnxruntime, tokenizers and numpy are imported lazily (inside the Onnx classes and the pooling helpers), so
importing this module, the API and the FakeEmbedder never loads a machine-learning library. Nothing here
imports torch, transformers or huggingface_hub, and nothing opens a network connection.

Memory (design C3, UNVERIFIED until `bench_embed` runs): about 0.5 GB resident per process for e5-small fp32
(the 470 MB ONNX weights plus the 17 MB tokenizer), plus activations that grow with the batch size.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


class EmbedderError(Exception):
    """Base class for embedding failures."""


class ModelOutputError(EmbedderError):
    """The model returned something unusable (wrong shape or dimension, NaN or infinity)."""


@runtime_checkable
class Embedder(Protocol):
    model_id: str  # "intfloat/multilingual-e5-small@<commit-sha>"
    dim: int  # 384
    max_tokens: int  # 512

    def count_tokens(self, text: str) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...  # adds the passage prefix

    def embed_query(self, text: str) -> list[float]: ...  # adds the query prefix


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _check_texts(texts: Sequence[str]) -> None:
    for t in texts:
        if not isinstance(t, str):
            raise TypeError(f"texts must be str, got {type(t).__name__}")


# ── Pooling and normalisation (numpy) ─────────────────────────────────────────


def mean_pool(hidden: Any, mask: Any) -> Any:
    """Average the token vectors over the real tokens only: hidden is (batch, seq, dim), mask (batch, seq)."""
    import numpy as np

    m = mask.astype(np.float32)[:, :, None]
    total = (hidden.astype(np.float32, copy=False) * m).sum(axis=1)
    count = np.maximum(m.sum(axis=1), 1.0)
    return total / count


def cls_pool(hidden: Any) -> Any:
    """The first token's vector (the `<s>` / CLS position)."""
    import numpy as np

    return hidden[:, 0, :].astype(np.float32, copy=False)


def l2_normalize(vectors: Any) -> Any:
    """Scale each row to unit length. An all-zero row stays zero (finite) instead of becoming NaN."""
    import numpy as np

    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.maximum(norms, 1e-12)


# ── ONNX embedders ────────────────────────────────────────────────────────────


class _Tokenizer:
    """A small wrapper around `tokenizers.Tokenizer` loaded from `tokenizer.json` only.

    Truncation and padding are configured explicitly here instead of trusting whatever the JSON file contains:
    truncation at `max_tokens` (special tokens included) and padding to the longest text of the batch. A second
    instance without truncation or padding counts tokens for the chunker.
    """

    def __init__(self, tokenizer_path: Path, max_tokens: int, pad_token: str) -> None:
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(str(tokenizer_path))
        pad_id = tok.token_to_id(pad_token)
        if pad_id is None:
            raise EmbedderError(f"the tokenizer has no {pad_token!r} token")
        tok.no_truncation()
        tok.no_padding()
        tok.enable_truncation(max_length=max_tokens)
        tok.enable_padding(pad_id=pad_id, pad_token=pad_token, direction="right")
        counter = Tokenizer.from_file(str(tokenizer_path))
        counter.no_truncation()
        counter.no_padding()
        self._tok = tok
        self._counter = counter

    def encode_batch(self, texts: list[str]) -> list[Any]:
        return list(self._tok.encode_batch(texts, add_special_tokens=True))

    def count(self, text: str) -> int:
        return len(self._counter.encode(text, add_special_tokens=False).ids)


class OnnxEmbedderBase:
    """Shared machinery. Subclasses fix the pooling and the prefixes."""

    pooling: str = ""
    query_prefix: str = ""
    passage_prefix: str = ""

    def __init__(
        self,
        model_path: Path,
        tokenizer_path: Path,
        *,
        model_id: str,
        dim: int,
        max_tokens: int = 512,
        batch_size: int = 16,
        intra_op_threads: int = 1,
        pad_token: str = "<pad>",
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if intra_op_threads < 1:
            raise ValueError("intra_op_threads must be at least 1")
        if dim < 1 or max_tokens < 3:
            raise ValueError("dim must be positive and max_tokens at least 3")
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3  # errors only
        # No custom-op library is ever registered, and only the CPU provider is requested.
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        names = [i.name for i in self.session.get_inputs()]
        unknown = set(names) - {"input_ids", "attention_mask", "token_type_ids"}
        if unknown or not {"input_ids", "attention_mask"} <= set(names):
            raise EmbedderError(f"unsupported model inputs {names}")
        self._wants_token_types = "token_type_ids" in names
        outputs = [o.name for o in self.session.get_outputs()]
        self._output_name = "last_hidden_state" if "last_hidden_state" in outputs else outputs[0]
        self._tok = _Tokenizer(tokenizer_path, max_tokens, pad_token)
        self.model_id = model_id
        self.dim = dim
        self.max_tokens = max_tokens
        self.batch_size = batch_size
        self._truncated = 0
        self._count_lock = threading.Lock()

    @property
    def truncated_count(self) -> int:
        """How many texts were cut at `max_tokens` since this embedder was created."""
        return self._truncated

    def count_tokens(self, text: str) -> int:
        """Tokens of the NFC-normalised text itself: no prefix, no special tokens, no truncation. The chunker
        keeps its budget below `max_tokens` minus the prefix and the two special tokens."""
        return self._tok.count(nfc(text))

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        encodings = self._tok.encode_batch(texts)
        ids = np.array([e.ids for e in encodings], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
        truncated = sum(1 for e in encodings if e.overflowing)
        if truncated:
            with self._count_lock:
                self._truncated += truncated
        feed = {"input_ids": ids, "attention_mask": mask}
        if self._wants_token_types:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run([self._output_name], feed)[0]
        if hidden.ndim != 3 or hidden.shape[0] != len(texts) or hidden.shape[2] != self.dim:
            raise ModelOutputError(
                f"the model returned shape {tuple(hidden.shape)}, expected (batch={len(texts)}, seq, dim={self.dim})"
            )
        pooled = mean_pool(hidden, mask) if self.pooling == "mean" else cls_pool(hidden)
        vectors = l2_normalize(pooled.astype(np.float32, copy=False))
        if not np.isfinite(vectors).all():
            raise ModelOutputError("the model returned NaN or infinite values")
        result: list[list[float]] = vectors.tolist()
        return result

    def _embed(self, prefix: str, texts: Sequence[str]) -> list[list[float]]:
        _check_texts(texts)
        prepared = [prefix + nfc(t) for t in texts]  # the one place a prefix is added
        out: list[list[float]] = []
        for start in range(0, len(prepared), self.batch_size):
            out.extend(self._embed_batch(prepared[start : start + self.batch_size]))
        return out

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(self.passage_prefix, texts)

    def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__}")
        return self._embed(self.query_prefix, [text])[0]


class OnnxE5Embedder(OnnxEmbedderBase):
    """intfloat/multilingual-e5-small (or base): mean pooling, `query: ` and `passage: ` prefixes (required)."""

    pooling = "mean"
    query_prefix = "query: "
    passage_prefix = "passage: "


class OnnxBgeM3Embedder(OnnxEmbedderBase):
    """BAAI/bge-m3 (the re-index upgrade path): CLS pooling, no prefix."""

    pooling = "cls"
    query_prefix = ""
    passage_prefix = ""


# ── FakeEmbedder ──────────────────────────────────────────────────────────────

# Letters, digits and underscore, plus the whole Devanagari block except the danda signs: Python's \w does not
# match Devanagari vowel signs (category Mc/Mn), so a Hindi word would otherwise fall apart at every matra.
_WORD = re.compile(r"[\wऀ-ॣ०-ॿ]+")


class FakeEmbedder:
    """Deterministic feature hashing of lower-cased word unigrams and bigrams into `dim` buckets, then L2.

    It has real lexical similarity (texts that share words and word pairs are close), needs no model file and
    no ML library, and gives the same vectors in every process (hashlib, never the salted built-in `hash`).
    Same interface and prefix behaviour as the real embedders: the strings it received, prefix included, are
    recorded in `received_queries` and `received_passages`. The vector itself is computed from the text
    without the prefix, so the prefix word does not add noise to the lexical similarity.
    """

    def __init__(
        self, dim: int = 384, max_tokens: int = 512, batch_size: int = 16, model_id: str | None = None
    ) -> None:
        if dim < 1 or max_tokens < 1 or batch_size < 1:
            raise ValueError("dim, max_tokens and batch_size must be positive")
        self.dim = dim
        self.max_tokens = max_tokens
        self.batch_size = batch_size
        self.model_id = model_id or f"fake-hash-{dim}"
        self.query_prefix = "query: "
        self.passage_prefix = "passage: "
        self.received_queries: list[str] = []
        self.received_passages: list[str] = []
        self._truncated = 0

    @property
    def truncated_count(self) -> int:
        return self._truncated

    @staticmethod
    def _words(text: str) -> list[str]:
        return _WORD.findall(nfc(text).lower())

    def count_tokens(self, text: str) -> int:
        return len(self._words(text))

    def _vector(self, text: str) -> list[float]:
        words = self._words(text)
        if len(words) > self.max_tokens:
            words = words[: self.max_tokens]
            self._truncated += 1
        features = [*words, *(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))]
        if not features:
            features = ["<empty>"]  # keep a valid unit vector for empty text
        vec = [0.0] * self.dim
        for feature in features:
            n = int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")
            vec[n % self.dim] += -1.0 if (n >> 32) & 1 else 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:  # features cancelled out exactly: fall back to one fixed bucket
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        _check_texts(texts)
        self.received_passages.extend(self.passage_prefix + nfc(t) for t in texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str):
            raise TypeError(f"text must be str, got {type(text).__name__}")
        self.received_queries.append(self.query_prefix + nfc(text))
        return self._vector(text)
