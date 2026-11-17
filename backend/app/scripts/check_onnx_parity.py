"""
python -m app.scripts.check_onnx_parity [--model e5-small] [--model-dir DIR] [--sentences FILE] [--threshold 0.999]

A one-time, MANUAL check (design A1: not a test, not part of CI) that the production embedder and a reference
computed another way agree on real sentences in English, Hindi and Marathi. Run it after `fetch_model` and
whenever the model, onnxruntime or tokenizers version changes.

The reference shares the tokenizer file and the ONNX weights with `OnnxE5Embedder` but nothing else: it opens its
own session, tokenizes ONE text at a time with no padding (so batching and padding cannot hide a bug) and pools
in plain Python (a second implementation of the pooling and of the L2 normalisation). It checks the passage
path (the `passage: ` prefix) for each sentence. The sentences come from tests/fixtures/parity_sentences.txt
(`<lang>|<sentence>` per line). It exits 0 when every cosine is above the threshold, 1 otherwise, and 2 when the
model files are missing or fail verification (with instructions).
"""

from __future__ import annotations

import argparse
import math
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from app.ml.embedder import Embedder
from app.ml.lock import DEFAULT_LOCK_PATH, ModelLoadError, ModelSpec, load_lock

DEFAULT_SENTENCES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "parity_sentences.txt"
LANGUAGES = ("en", "hi", "mr")


@dataclass(frozen=True)
class ParityResult:
    lang: str
    text: str
    cosine: float


@dataclass(frozen=True)
class ParityReport:
    results: list[ParityResult]
    threshold: float

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.cosine > self.threshold for r in self.results)


def read_sentences(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        lang, sep, text = line.partition("|")
        if not sep or lang not in LANGUAGES or not text.strip():
            raise ValueError(f"{path}:{number}: expected '<en|hi|mr>|<sentence>'")
        rows.append((lang, text.strip()))
    return rows


def reference_embeddings(spec: ModelSpec, directory: Path, texts: list[str]) -> list[list[float]]:
    """Passage embeddings through a separate session, one text at a time, with pooling written out by hand."""
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    assert spec.model_file and spec.tokenizer_file
    tok = Tokenizer.from_file(str(directory / spec.tokenizer_file))
    tok.no_padding()
    tok.enable_truncation(max_length=spec.max_tokens)
    session = ort.InferenceSession(str(directory / spec.model_file), providers=["CPUExecutionProvider"])
    names = {i.name for i in session.get_inputs()}
    out_name = "last_hidden_state" if "last_hidden_state" in {o.name for o in session.get_outputs()} else None
    prefix = spec.prefixes["passage"]
    result: list[list[float]] = []
    for text in texts:
        ids = tok.encode(prefix + unicodedata.normalize("NFC", text)).ids
        feed = {"input_ids": np.array([ids], dtype=np.int64), "attention_mask": np.ones((1, len(ids)), dtype=np.int64)}
        if "token_type_ids" in names:
            feed["token_type_ids"] = np.zeros((1, len(ids)), dtype=np.int64)
        hidden = session.run([out_name] if out_name else None, feed)[0][0].tolist()  # seq x dim, plain lists
        if spec.pooling == "mean":
            dim = len(hidden[0])
            pooled = [sum(row[d] for row in hidden) / len(hidden) for d in range(dim)]
        else:
            pooled = list(hidden[0])
        norm = math.sqrt(sum(x * x for x in pooled)) or 1.0
        result.append([x / norm for x in pooled])
    return result


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def compare(
    embedder: Embedder, spec: ModelSpec, directory: Path, rows: list[tuple[str, str]], *, threshold: float
) -> ParityReport:
    texts = [t for _, t in rows]
    ours = embedder.embed_passages(texts)
    theirs = reference_embeddings(spec, directory, texts)
    results = [ParityResult(lang, text, _cosine(a, b)) for (lang, text), a, b in zip(rows, ours, theirs, strict=True)]
    return ParityReport(results=results, threshold=threshold)


def missing_model_help(name: str, reason: str) -> str:
    return (
        f"cannot check parity: {reason}\n"
        f"Fetch and verify the model first:\n"
        f"  python -m app.scripts.fetch_model {name} --out <model dir>\n"
        f"then pass --model-dir <model dir> or set EMBEDDING_MODEL_DIR=<model dir>."
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.scripts.check_onnx_parity", description=__doc__.split("\n\n")[0])
    p.add_argument("--model", default="e5-small", help="model name in the lock file")
    p.add_argument("--lock", help="lock file (default: the packaged one)")
    p.add_argument("--model-dir", help="model root directory (default: EMBEDDING_MODEL_DIR)")
    p.add_argument("--sentences", default=str(DEFAULT_SENTENCES))
    p.add_argument("--threshold", type=float, default=0.999)
    args = p.parse_args(argv)

    model_dir = args.model_dir
    if not model_dir:
        from app.core.config import settings

        model_dir = settings.EMBEDDING_MODEL_DIR
    if not model_dir:
        print(missing_model_help(args.model, "no model directory (--model-dir or EMBEDDING_MODEL_DIR)"), file=sys.stderr)
        return 2
    try:
        from app.ml.loader import load_embedder

        lock_path = Path(args.lock) if args.lock else DEFAULT_LOCK_PATH
        spec = load_lock(lock_path).get(args.model)
        embedder = load_embedder(args.model, Path(model_dir), lock_path=lock_path)
    except ModelLoadError as exc:
        print(missing_model_help(args.model, str(exc)), file=sys.stderr)
        return 2

    rows = read_sentences(Path(args.sentences))
    report = compare(embedder, spec, Path(model_dir) / spec.directory_name, rows, threshold=args.threshold)
    for lang in LANGUAGES:
        scores = [r.cosine for r in report.results if r.lang == lang]
        if scores:
            print(f"{lang}: {len(scores)} sentences, min cosine {min(scores):.6f}, mean {sum(scores) / len(scores):.6f}")
    for r in report.results:
        if r.cosine <= report.threshold:
            print(f"  BELOW THRESHOLD {r.cosine:.6f} [{r.lang}] {r.text}")
    verdict = "PASS" if report.passed else "FAIL"
    print(f"{verdict}: {len(report.results)} sentences, threshold cosine > {report.threshold}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
