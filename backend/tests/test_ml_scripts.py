"""
Step 5c-1: the operator scripts `check_onnx_parity` and `bench_embed`. They are NOT tests and not part of CI; their
logic is exercised here with the FakeEmbedder and the synthetic ONNX model, and the real runs stay manual.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from app.ml.embedder import FakeEmbedder, OnnxE5Embedder
from app.scripts import bench_embed, check_onnx_parity
from tests.fixtures.ml import DIM, SyntheticModel

pytestmark = pytest.mark.usefixtures("block_sockets")

SENTENCES = Path(__file__).resolve().parent / "fixtures" / "parity_sentences.txt"


def _lock_and_dir(m: SyntheticModel, tmp_path: Path) -> tuple[Path, str]:
    name = m.root.name
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"version": 1, "models": {name: m.entry}}), encoding="utf-8")
    return lock, name


# ── Parity sentences file ─────────────────────────────────────────────────────


def test_the_parity_sentence_file_has_fifty_nfc_sentences_in_three_languages() -> None:
    rows = check_onnx_parity.read_sentences(SENTENCES)
    assert len(rows) == 50
    by_lang: dict[str, int] = {}
    for lang, text in rows:
        by_lang[lang] = by_lang.get(lang, 0) + 1
        assert text.strip() == text and len(text) > 20
        assert unicodedata.normalize("NFC", text) == text
    assert set(by_lang) == {"en", "hi", "mr"} and min(by_lang.values()) >= 15
    assert len({t for _, t in rows}) == 50  # no duplicates
    for lang, text in rows:
        has_deva = any("ऀ" <= ch <= "ॿ" for ch in text)
        assert has_deva == (lang in {"hi", "mr"}), text


def test_read_sentences_refuses_a_malformed_file(tmp_path: Path) -> None:
    bad = tmp_path / "s.txt"
    bad.write_text("# c\nen|ok sentence here\nfr|bonjour\n", encoding="utf-8")
    with pytest.raises(ValueError):
        check_onnx_parity.read_sentences(bad)
    bad.write_text("no separator\n", encoding="utf-8")
    with pytest.raises(ValueError):
        check_onnx_parity.read_sentences(bad)


# ── Parity check logic ────────────────────────────────────────────────────────


def test_the_independent_reference_agrees_with_the_embedder_on_the_synthetic_model(
    synthetic_e5: SyntheticModel, tmp_path: Path
) -> None:
    lock, name = _lock_and_dir(synthetic_e5, tmp_path)
    from app.ml.lock import load_lock

    spec = load_lock(lock).get(name)
    emb = OnnxE5Embedder(synthetic_e5.model_path, synthetic_e5.tokenizer_path, model_id="x@0", dim=DIM)
    rows = [("en", "the cat sat on the mat"), ("hi", "भारत की राजधानी नई दिल्ली है"), ("mr", "शाळा विद्यार्थी आहे")]
    report = check_onnx_parity.compare(emb, spec, synthetic_e5.root, rows, threshold=0.999)
    assert report.passed and len(report.results) == 3
    assert all(r.cosine > 0.999 for r in report.results)
    assert {r.lang for r in report.results} == {"en", "hi", "mr"}


def test_the_parity_check_fails_when_the_embedder_is_wrong(synthetic_e5: SyntheticModel, tmp_path: Path) -> None:
    lock, name = _lock_and_dir(synthetic_e5, tmp_path)
    from app.ml.lock import load_lock

    spec = load_lock(lock).get(name)
    emb = OnnxE5Embedder(synthetic_e5.model_path, synthetic_e5.tokenizer_path, model_id="x@0", dim=DIM)
    original = emb.embed_passages
    emb.embed_passages = lambda texts: [list(reversed(v)) for v in original(texts)]  # type: ignore[method-assign]
    report = check_onnx_parity.compare(emb, spec, synthetic_e5.root, [("en", "the cat sat on the mat")], threshold=0.999)
    assert not report.passed


def test_parity_main_prints_instructions_when_the_model_files_are_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBEDDING_MODEL_DIR", str(tmp_path / "empty"))
    rc = check_onnx_parity.main(["--model-dir", str(tmp_path / "empty")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "python -m app.scripts.fetch_model e5-small" in err and "EMBEDDING_MODEL_DIR" in err


def test_parity_main_passes_on_the_synthetic_model_end_to_end(
    synthetic_e5: SyntheticModel, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock, name = _lock_and_dir(synthetic_e5, tmp_path)
    sentences = tmp_path / "s.txt"
    sentences.write_text("en|the cat sat on the mat today\nhi|भारत की राजधानी नई दिल्ली है आज\n", encoding="utf-8")
    rc = check_onnx_parity.main(["--model", name, "--lock", str(lock), "--model-dir", str(synthetic_e5.root.parent),
                                 "--sentences", str(sentences)])
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


# ── Benchmark logic ───────────────────────────────────────────────────────────


def test_peak_rss_is_a_positive_number_of_bytes() -> None:
    rss = bench_embed.peak_rss_bytes()
    assert isinstance(rss, int) and rss > 10_000_000  # a Python process is far above 10 MB


def test_the_benchmark_measures_every_batch_size_and_prints_a_table() -> None:
    texts = [f"the cat sat on mat number {i} " * 5 for i in range(40)]
    rows = bench_embed.run_bench(FakeEmbedder(dim=32), texts, batch_sizes=(1, 8, 16, 32))
    assert [r.batch_size for r in rows] == [1, 8, 16, 32]
    for r in rows:
        assert r.texts_per_second > 0 and r.tokens_per_second > 0 and r.peak_rss_bytes > 0
        assert r.texts == 40 and r.tokens > 0
    table = bench_embed.format_table(rows)
    assert "batch" in table and "texts/s" in table and "tokens/s" in table and "peak RSS" in table
    assert len(table.splitlines()) >= 2 + 4


def test_the_benchmark_restores_the_batch_size_it_changed() -> None:
    e: Any = FakeEmbedder(dim=8, batch_size=5)
    bench_embed.run_bench(e, ["a b c"] * 4, batch_sizes=(1, 2))
    assert e.batch_size == 5


def test_bench_main_prints_instructions_when_the_model_files_are_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = bench_embed.main(["--model-dir", str(tmp_path / "empty")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "python -m app.scripts.fetch_model e5-small" in err and "EMBEDDING_MODEL_DIR" in err


def test_bench_main_runs_against_the_synthetic_model(
    synthetic_e5: SyntheticModel, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock, name = _lock_and_dir(synthetic_e5, tmp_path)
    rc = bench_embed.main(["--model", name, "--lock", str(lock), "--model-dir", str(synthetic_e5.root.parent),
                           "--texts", "24", "--batch-sizes", "1,8"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "texts/s" in out and len(out.strip().splitlines()) >= 4
