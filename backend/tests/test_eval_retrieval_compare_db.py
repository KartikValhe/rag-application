"""
tests/test_eval_retrieval_compare_db.py
───────────────────────────────────────
`eval_retrieval --compare` (step 5d-4) on SYNTHETIC documents: the test database, the academic_world fixture
and a scripted embedder. The real golden set and the real model are never used, and the harness is never run
against the dev database by a test.

  * it runs the golden set once per configuration through the SAME retrieval.search, and prints one table;
  * every question is embedded ONCE, however many configurations run;
  * it stays READ ONLY, changes no setting and no threshold;
  * the "current settings" row equals what a normal run of the harness reports;
  * the report says how many should-answer / should-not-answer rows there are, warns when they are too few,
    and counts the questions whose hit@1 changed against the current settings;
  * a bad --grid is an error before the model is loaded.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.scripts import eval_retrieval
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.retrieval_world import ScriptedEmbedder
from tests.test_eval_retrieval_db import (
    CHUNK_TEXTS,
    QUESTIONS,
    args,
    never_load,
    scripted_embedder,
    seed_world,
    use_the_shipped_defaults,
)


@pytest.fixture(autouse=True)
def _threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SIMILARITY", 0.85)
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_KEYWORD_COVERAGE", 0.0)
    monkeypatch.setattr(settings, "RETRIEVAL_KEYWORD_MAX_DF", 1.0)
    monkeypatch.setattr(settings, "RETRIEVAL_RRF_VECTOR_WEIGHT", 1.0)
    monkeypatch.setattr(settings, "RETRIEVAL_RRF_KEYWORD_WEIGHT", 1.0)
    monkeypatch.setattr(settings, "RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS", 1)


SMALL_GRID = ["--grid", "vw=1,2", "df=off,0.5"]


def read(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


async def test_compare_runs_every_configuration_through_one_read_only_session_and_prints_one_table(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    embedder: ScriptedEmbedder = scripted_embedder()
    seen: list[str] = []
    real = eval_retrieval.readonly_tenant_session

    @asynccontextmanager
    async def spy(tenant_id: Any) -> AsyncIterator[AsyncSession]:
        async with real(tenant_id) as db:
            seen.append((await db.execute(text("SHOW transaction_read_only"))).scalar_one())
            yield db

    monkeypatch.setattr(eval_retrieval, "readonly_tenant_session", spy)
    await raw_db.commit()
    code = await eval_retrieval.run(args(w, tmp_path, "--compare", *SMALL_GRID), embedder_loader=lambda: embedder)
    out = capsys.readouterr().out
    assert code == 0, out
    assert seen == ["on"], "one READ ONLY transaction for the whole comparison"

    report = read(tmp_path)
    assert [r["role"] for r in report["runs"]] == ["current", "baseline", "grid", "grid", "grid", "grid"]
    assert report["runs"][1]["keyword_weight"] == 0.0 and report["runs"][1]["label"] == "vector only"
    assert {(r["vector_weight"], r["keyword_max_df"]) for r in report["runs"][2:]} == {(1.0, 1.0), (1.0, 0.5), (2.0, 1.0), (2.0, 0.5)}

    # every question embedded once, not once per configuration
    assert sorted(embedder.received_queries) == sorted(q[2] for q in QUESTIONS)

    assert report["sample"]["should_answer"] == 5 and report["sample"]["should_not_answer"] == 3
    assert len(report["sample"]["warnings"]) == 2
    assert out.count("too few to trust small differences") == 2
    assert "5 should-answer" in out and "3 should-not-answer" in out
    for fragment in ("current settings", "vector only", "vw=2 kw=1 df=0.5 mt=1", "English", "Marathi", "hit@1", "MRR",
                     "answered", "refused", "RETRIEVAL_MIN_SIMILARITY = 0.85", "calibrated on 2026-11-20"):
        assert fragment in out, fragment
    # ids, titles and numbers only
    written = (tmp_path / "report.json").read_text(encoding="utf-8")
    for body in CHUNK_TEXTS:
        assert body not in out and body not in written
    assert all("questions" not in r for r in report["runs"])


async def test_the_current_row_equals_a_normal_run_and_an_identical_grid_point_changes_nothing(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path), embedder_loader=scripted_embedder) == 0
    normal = read(tmp_path)
    assert await eval_retrieval.run(args(w, tmp_path, "--compare", *SMALL_GRID), embedder_loader=scripted_embedder) == 0
    compare = read(tmp_path)
    current = compare["runs"][0]
    assert current["role"] == "current" and (current["hit1_wins"], current["hit1_losses"]) == (0, 0)
    for key in ("hit_at", "mrr", "answer_rate", "refusal_accuracy", "should_answer", "should_not_answer"):
        assert current["overall"][key] == normal["overall"][key], key
    assert current["by_language"] == normal["by_language"]
    same = next(r for r in compare["runs"] if r["role"] == "grid" and (r["vector_weight"], r["keyword_max_df"]) == (1.0, 1.0))
    assert same["overall"] == current["overall"] and (same["hit1_wins"], same["hit1_losses"]) == (0, 0)


async def test_the_vector_only_baseline_ranks_exactly_like_the_vector_side(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    """Keyword weight 0: the fused hit@k are the vector-only hit@k of the current run, and the win and loss
    counts against the current settings stay within the number of should-answer questions."""
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path, "--compare", *SMALL_GRID), embedder_loader=scripted_embedder) == 0
    report = read(tmp_path)
    current, baseline = report["runs"][0], report["runs"][1]
    assert baseline["overall"]["hit_at"] == current["overall"]["vector_only_hit_at"]
    assert 0 <= baseline["hit1_wins"] + baseline["hit1_losses"] <= report["sample"]["should_answer"]


async def test_the_default_grid_has_thirty_points_plus_the_current_and_baseline_rows(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path, "--compare"), embedder_loader=scripted_embedder) == 0
    report = read(tmp_path)
    assert len(report["runs"]) == 1 + 1 + 3 * 5 * 2
    assert len({r["label"] if r["role"] != "grid" else (r["vector_weight"], r["keyword_max_df"], r["keyword_min_matched_terms"]) for r in report["runs"]}) == 32


async def test_compare_reads_the_current_settings_and_changes_none(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    monkeypatch.setattr(settings, "RETRIEVAL_KEYWORD_MAX_DF", 0.5)
    monkeypatch.setattr(settings, "RETRIEVAL_RRF_VECTOR_WEIGHT", 2.0)
    assert await eval_retrieval.run(args(w, tmp_path, "--compare", *SMALL_GRID), embedder_loader=scripted_embedder) == 0
    report = read(tmp_path)
    current = report["runs"][0]
    assert (current["keyword_max_df"], current["vector_weight"], current["keyword_weight"]) == (0.5, 2.0, 1.0)
    # the scripted embedder carries the e5-small key, the model the threshold was calibrated for (5d-5)
    assert report["meta"]["threshold"] == 0.85 and report["meta"]["threshold_calibrated"] is True
    assert settings.RETRIEVAL_KEYWORD_MAX_DF == 0.5 and settings.RETRIEVAL_RRF_VECTOR_WEIGHT == 2.0
    assert settings.RETRIEVAL_MIN_SIMILARITY == 0.85


@pytest.mark.parametrize("grid", [["vw=0"], ["df=2"], ["color=red"], ["vw=a"]])
async def test_a_bad_grid_is_an_error_before_the_model_is_loaded(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    grid: list[str],
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path, "--compare", "--grid", *grid), embedder_loader=never_load) == 2
    assert "grid" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


async def test_grid_without_compare_is_an_error(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path, *SMALL_GRID), embedder_loader=never_load) == 2
    assert "--compare" in capsys.readouterr().err


async def test_a_normal_run_is_unchanged_and_still_prints_the_old_report(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path), embedder_loader=scripted_embedder) == 0
    out = capsys.readouterr().out
    assert "=== Retrieval evaluation ===" in out and "Threshold sweep" in out and "runs" not in read(tmp_path)


async def test_with_the_shipped_defaults_the_current_row_is_the_vector_only_row(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5d-5: "current settings" uses the new defaults (keyword weight 0), so it ranks exactly like the
    vector-only baseline; the grid rows still try the hybrid (keyword weight 1)."""
    w, _, _ = await seed_world(raw_db, academic_world)
    use_the_shipped_defaults(monkeypatch)
    assert await eval_retrieval.run(args(w, tmp_path, "--compare", *SMALL_GRID), embedder_loader=scripted_embedder) == 0
    out = capsys.readouterr().out
    report = read(tmp_path)
    current, baseline = report["runs"][0], report["runs"][1]
    assert (current["keyword_weight"], current["vector_weight"], current["keyword_max_df"]) == (0.0, 1.0, 1.0)
    assert report["meta"]["current_settings"]["rrf_keyword_weight"] == 0.0
    assert current["overall"] == baseline["overall"] and (baseline["hit1_wins"], baseline["hit1_losses"]) == (0, 0)
    assert all(r["keyword_weight"] == 1.0 for r in report["runs"][2:])
    assert report["meta"]["threshold"] == 0.84 and report["meta"]["threshold_calibrated"] is True
    assert "calibrated on 2026-11-20 for" in out and "not calibrated" not in out
