"""
tests/test_retrieval_compare_pure.py
────────────────────────────────────
Step 5d-4, the pure parts of `eval_retrieval --compare`: the grid builder and its small custom form, the
"how many questions changed their hit@1 against the current settings" counter, the sample-size guard and
the one-table report. No database, no model.
"""

from __future__ import annotations

import uuid

import pytest

from app.scripts import eval_retrieval
from app.services import retrieval_eval as ev


def result(number: int, language: str, *, should_answer: bool = True, hit: int | None = 1, sufficient: bool = True) -> ev.QuestionResult:
    return ev.QuestionResult(
        number=str(number), row=number + 1, language=language, question="q", document="D" if should_answer else None,
        expected_location="page 1", should_answer=should_answer, sufficient=sufficient, reason="similarity",
        max_similarity=0.9, keyword_coverage=None, candidate_count=10, hit_position=hit if should_answer else None,
        vector_hit_position=hit if should_answer else None, keyword_hit_position=None,
        outcome=ev.OK if should_answer else ev.REFUSED_CORRECTLY, top=(),
    )


# ── the grid ─────────────────────────────────────────────────────────────────


def test_the_default_grid_is_vector_weight_times_max_df_times_min_terms() -> None:
    grid = ev.build_grid()
    assert len(grid) == 3 * 5 * 2
    assert {p.vector_weight for p in grid} == {1.0, 2.0, 3.0}
    assert {p.keyword_max_df for p in grid} == {1.0, 0.5, 0.3, 0.2, 0.1}
    assert {p.keyword_min_matched_terms for p in grid} == {1, 2}
    assert {p.keyword_weight for p in grid} == {1.0}
    assert len(set(grid)) == len(grid), "no duplicate configuration"
    assert grid[0] == ev.GridPoint(1.0, 1.0, 1.0, 1), "the first point is the neutral one"


def test_labels_say_what_the_point_is() -> None:
    assert ev.GridPoint(2.0, 1.0, 0.3, 2).label == "vw=2 kw=1 df=0.3 mt=2"
    assert ev.GridPoint(1.0, 1.0, 1.0, 1).label == "vw=1 kw=1 df=off mt=1"
    assert ev.vector_only_point().label == "vw=1 kw=0 df=off mt=1"
    assert len({p.label for p in ev.build_grid()}) == 30


def test_a_custom_grid_names_only_what_it_varies() -> None:
    grid = ev.parse_grid(["vw=1,2", "df=off,0.3"])
    assert [(p.vector_weight, p.keyword_max_df, p.keyword_min_matched_terms) for p in grid] == [
        (1.0, 1.0, 1), (1.0, 0.3, 1), (2.0, 1.0, 1), (2.0, 0.3, 1),
    ]
    assert ev.parse_grid(["mt=2"]) == [ev.GridPoint(1.0, 1.0, 1.0, 2)]
    assert ev.parse_grid(["vw=3", "df=0.5", "mt=1"]) == [ev.GridPoint(3.0, 1.0, 0.5, 1)]


@pytest.mark.parametrize(
    "tokens",
    [["vw"], ["vw="], ["vw=x"], ["vw=0"], ["vw=-1"], ["df=1.5"], ["df=-0.1"], ["df=nan"], ["mt=0"], ["mt=1.5"],
     ["color=red"], ["vw=1", "vw=2"], []],
)
def test_a_bad_custom_grid_is_refused(tokens: list[str]) -> None:
    with pytest.raises(ev.GridError):
        ev.parse_grid(tokens)


def test_the_grid_size_is_capped() -> None:
    with pytest.raises(ev.GridError, match="at most"):
        ev.parse_grid(["vw=" + ",".join(str(i) for i in range(1, 40)), "df=" + ",".join(f"0.{i}" for i in range(1, 10)), "mt=1,2,3"])


# ── how many answers changed ─────────────────────────────────────────────────


def test_hit1_changes_count_wins_and_losses_among_should_answer_rows() -> None:
    reference = [result(1, "English", hit=1), result(2, "English", hit=2), result(3, "Marathi", hit=None),
                 result(4, "Marathi", hit=1), result(5, "English", should_answer=False)]
    other = [result(1, "English", hit=2), result(2, "English", hit=1), result(3, "Marathi", hit=1),
             result(4, "Marathi", hit=1), result(5, "English", should_answer=False, sufficient=False)]
    assert ev.hit1_changes(reference, other) == (2, 1)  # questions 2 and 3 gained, question 1 lost
    assert ev.hit1_changes(reference, reference) == (0, 0)


def test_hit1_changes_refuse_results_of_different_questions() -> None:
    with pytest.raises(ValueError):
        ev.hit1_changes([result(1, "English")], [result(2, "English")])
    with pytest.raises(ValueError):
        ev.hit1_changes([result(1, "English")], [])


# ── the overfitting guard ────────────────────────────────────────────────────


def test_a_small_golden_set_gets_a_warning_for_each_kind_that_is_too_small() -> None:
    assert ev.sample_warnings(20, 8) == []
    both = ev.sample_warnings(19, 7)
    assert len(both) == 2 and all("too few to trust small differences" in w for w in both)
    assert "19" in both[0] and "20" in both[0] and "7" in both[1] and "8" in both[1]
    assert len(ev.sample_warnings(19, 8)) == 1
    assert len(ev.sample_warnings(20, 0)) == 1, "no should-not-answer row at all is also too few"


# ── the report ───────────────────────────────────────────────────────────────


def _runs() -> list[ev.ConfigRun]:
    english = [result(i, "English", hit=1) for i in range(1, 5)]
    marathi = [result(i, "Marathi", hit=None if i == 5 else 1) for i in range(5, 7)]
    refusals = [result(7, "English", should_answer=False)]
    current = english + marathi + refusals
    better = [result(int(r.number), r.language, hit=1) if r.should_answer else r for r in current]
    worse = [result(int(r.number), r.language, hit=3) if r.should_answer and r.language == "English" else r for r in current]
    return [
        ev.ConfigRun("current", "current settings", ev.GridPoint(1.0, 1.0, 1.0, 1), tuple(current)),
        ev.ConfigRun("baseline", "vector only", ev.vector_only_point(), tuple(worse)),
        ev.ConfigRun("grid", "grid", ev.GridPoint(2.0, 1.0, 0.3, 2), tuple(better)),
    ]


META = {"tenant": "acme", "class_id": "c", "subject_id": "s", "model": "m", "k": 10, "threshold": 0.85,
        "threshold_calibrated": False}


def test_the_compare_report_has_one_entry_per_configuration_with_changes_against_the_current_row() -> None:
    report = ev.build_compare_report(_runs(), cutoff=10, meta=META)
    assert [r["role"] for r in report["runs"]] == ["current", "baseline", "grid"]
    current, baseline, grid = report["runs"]
    assert (current["hit1_wins"], current["hit1_losses"]) == (0, 0)
    assert (baseline["hit1_wins"], baseline["hit1_losses"]) == (0, 4)  # the four English answers dropped to rank 3
    assert (grid["hit1_wins"], grid["hit1_losses"]) == (1, 0)  # question 5 gained
    assert set(grid["by_language"]) == {"English", "Marathi"}
    assert grid["overall"]["hit_at"]["1"] == 1.0 and current["overall"]["hit_at"]["1"] == pytest.approx(5 / 6)
    assert report["sample"] == {"should_answer": 6, "should_not_answer": 1, "warnings": ev.sample_warnings(6, 1)}
    assert grid["keyword_max_df"] == 0.3 and grid["vector_weight"] == 2.0 and grid["keyword_min_matched_terms"] == 2


def test_the_table_shows_the_sizes_the_warnings_and_every_row() -> None:
    text = ev.format_compare(ev.build_compare_report(_runs(), cutoff=10, meta=META))
    assert "6 should-answer" in text and "1 should-not-answer" in text
    assert text.count("too few to trust small differences") == 2
    for fragment in ("current settings", "vector only", "vw=2 kw=1 df=0.3 mt=2", "English", "Marathi", "hit@1", "hit@3",
                     "hit@5", "MRR", "+1/-0", "+0/-4", "+0/-0", "answered", "refused"):
        assert fragment in text, fragment
    assert "RETRIEVAL_MIN_SIMILARITY = 0.85" in text and "not calibrated" in text


def test_no_warning_when_the_golden_set_is_large_enough() -> None:
    rows = [result(i, "English") for i in range(1, 21)] + [result(i, "English", should_answer=False) for i in range(21, 29)]
    run = ev.ConfigRun("current", "current settings", ev.GridPoint(1.0, 1.0, 1.0, 1), tuple(rows))
    text = ev.format_compare(ev.build_compare_report([run], cutoff=10, meta=META))
    assert "too few" not in text and "20 should-answer" in text and "8 should-not-answer" in text


def test_the_compare_report_carries_no_question_text() -> None:
    report = ev.build_compare_report(_runs(), cutoff=10, meta=META)
    assert "questions" not in report["runs"][0], "numbers only: a comparison run keeps no per-question rows"


# ── the command line ─────────────────────────────────────────────────────────


def _parse(*extra: str) -> object:
    return eval_retrieval.build_parser().parse_args(
        ["--tenant", "t", "--class-id", str(uuid.uuid4()), "--subject-id", str(uuid.uuid4()), "--report", "r.json", *extra]
    )


def test_compare_is_off_by_default_and_takes_a_custom_grid() -> None:
    plain = _parse()
    assert plain.compare is False and plain.grid is None  # type: ignore[attr-defined]
    args = _parse("--compare", "--grid", "vw=1,2", "df=off,0.3")
    assert args.compare is True and args.grid == ["vw=1,2", "df=off,0.3"]  # type: ignore[attr-defined]
