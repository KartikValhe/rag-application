"""
tests/test_golden_set_fixture.py
────────────────────────────────
The golden set the threshold was calibrated on (step 5d-5) is a committed fixture: tests/fixtures/golden_set.csv.
This checks that the harness's own parser reads it, that it is large enough to trust (the same limits the
`--compare` warning uses), that it covers both languages, and that every expected location is in a format the
harness can match. No database, no model.
"""

from pathlib import Path

from app.scripts.eval_retrieval import DEFAULT_GOLDEN
from app.services import retrieval_eval as ev

GOLDEN = Path(__file__).parent / "fixtures" / "golden_set.csv"


def test_the_harness_default_points_at_this_fixture() -> None:
    assert Path(DEFAULT_GOLDEN).as_posix() == "tests/fixtures/golden_set.csv"
    assert (Path(__file__).parent.parent / DEFAULT_GOLDEN).resolve() == GOLDEN.resolve()


def test_the_golden_set_parses_with_the_harness_parser() -> None:
    rows = ev.load_golden(GOLDEN)
    answers = [r for r in rows if r.should_answer]
    refusals = [r for r in rows if not r.should_answer]
    assert len(answers) >= ev.MIN_ANSWER_ROWS == 20
    assert len(refusals) >= ev.MIN_REFUSAL_ROWS == 8
    assert not ev.sample_warnings(len(answers), len(refusals))
    assert len({r.number for r in rows}) == len(rows), "every row has its own number"


def test_the_golden_set_covers_both_languages_on_both_sides() -> None:
    rows = ev.load_golden(GOLDEN)
    assert {r.language for r in rows} == {"English", "Marathi"}
    for language in ("English", "Marathi"):
        assert any(r.should_answer and r.language == language for r in rows), language
        assert any(not r.should_answer and r.language == language for r in rows), language


def test_every_expected_location_is_in_a_supported_format() -> None:
    for row in ev.load_golden(GOLDEN):
        if row.should_answer:
            assert row.document, f"row {row.row}: no document"
            assert row.location is not None, f"row {row.row}"
            assert ev.parse_location(row.location_text) == row.location, f"row {row.row}: {row.location_text!r}"
            if row.location.kind == "pages":
                assert row.location.start is not None and row.location.end is not None
                assert 1 <= row.location.start <= row.location.end
            else:
                assert row.location.heading
        else:
            assert row.document is None, f"row {row.row}: a should-not-answer row names no document"
            assert ev.fold(row.location_text) in ("", ev.fold(ev.NONE_DOCUMENT)), f"row {row.row}"
