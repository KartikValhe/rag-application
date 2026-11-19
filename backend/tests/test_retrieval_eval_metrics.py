"""
tests/test_retrieval_eval_metrics.py
────────────────────────────────────
The pure part of the retrieval evaluation harness (step 5d-3, docs/phase5d-plan.md section 10):
golden-set loader, location matcher, hit@k, MRR, answer rate, refusal accuracy, per-language split, the
threshold sweep and its two summary thresholds. No database and no model: the retrieved chunks are made by
hand. The golden set here is SYNTHETIC (the real tests/fixtures/golden_set.csv belongs to the owner).
"""

import json
import unicodedata

import pytest

from app.services import retrieval_eval as ev

HEADER = "#,Language,Question,Document,Expected location,Should answer\n"


def golden(*rows: str, bom: bool = False, header: str = HEADER) -> str:
    return ("﻿" if bom else "") + header + "".join(r + "\n" for r in rows)


# ── golden-set loader ────────────────────────────────────────────────────────


def test_the_loader_reads_rows_with_and_without_a_bom() -> None:
    body = (
        "1,English,What is osmosis?,Biology Notes,page 3,Yes\n"
        '2,Marathi,"प्रकाशसंश्लेषण म्हणजे काय?",Biology Notes,heading: प्रकाश,yes\n'
        "3,English,Who won the 1999 cricket final?,(none),,No\n"
    )
    for bom in (False, True):
        rows = ev.parse_golden(golden(*body.splitlines(), bom=bom))
        assert [r.number for r in rows] == ["1", "2", "3"]
        assert [r.language for r in rows] == ["English", "Marathi", "English"]
        assert rows[1].question == "प्रकाशसंश्लेषण म्हणजे काय?"
        assert [r.should_answer for r in rows] == [True, True, False]
        assert [r.document for r in rows] == ["Biology Notes", "Biology Notes", None]
        assert rows[2].location is None
        assert [r.row for r in rows] == [2, 3, 4]  # row 1 is the header, like a spreadsheet


def test_the_loader_reads_a_file_with_a_bom_from_disk(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "golden.csv"
    path.write_bytes(golden("1,English,Q?,Doc,page 1,Yes", bom=True).encode("utf-8"))
    assert [r.question for r in ev.load_golden(path)] == ["Q?"]


def test_blank_rows_are_skipped_and_columns_are_found_by_name() -> None:
    reordered = "Question,Should answer,#,Document,Language,Expected location,Notes\n" \
                "Q one?,Yes,1,Doc,English,slide 2,free text\n,,,,,,\n"
    rows = ev.parse_golden(reordered)
    assert len(rows) == 1 and rows[0].number == "1" and rows[0].location == ev.Location("pages", 2, 2, None)


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        ("1,English,,Doc,page 1,Yes", "Question"),  # no question
        ("1,,Q?,Doc,page 1,Yes", "Language"),  # no language
        ("1,English,Q?,Doc,page 1,Maybe", "Should answer"),  # Should answer must be Yes or No
        ("1,English,Q?,Doc,sometime,Yes", "Expected location"),  # a location the matcher cannot read
        ("1,English,Q?,Doc,,Yes", "Expected location"),  # a should-answer row needs a location
        ("1,English,Q?,(none),,Yes", "(none)"),  # (none) contradicts Yes
        ("1,English,Q?,Doc,page 1", "columns"),  # too few columns
        ("1,English,Q, with a comma,Doc,page 1,Yes", "columns"),  # too many columns (an unquoted comma)
    ],
)
def test_a_malformed_row_is_a_clear_error_with_its_row_number(row: str, needle: str) -> None:
    with pytest.raises(ev.GoldenSetError) as only:
        ev.parse_golden(golden(row))
    assert only.value.row == 2 and "row 2" in str(only.value) and needle in str(only.value)
    with pytest.raises(ev.GoldenSetError) as exc:
        ev.parse_golden(golden("1,English,Fine?,Doc,page 1,Yes", row))
    assert exc.value.row == 3 and "row 3" in str(exc.value), "the bad row is the third row of the file"


def test_a_missing_column_or_an_empty_file_is_a_clear_error() -> None:
    with pytest.raises(ev.GoldenSetError, match="Expected location"):
        ev.parse_golden("#,Language,Question,Document,Should answer\n1,English,Q?,Doc,Yes\n")
    with pytest.raises(ev.GoldenSetError, match="no header"):
        ev.parse_golden("")
    with pytest.raises(ev.GoldenSetError, match="no questions"):
        ev.parse_golden(HEADER)


def test_a_file_that_is_not_utf8_is_a_clear_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "bad.csv"
    path.write_bytes(HEADER.encode() + b"1,English,Caf\xe9?,Doc,page 1,Yes\n")
    with pytest.raises(ev.GoldenSetError, match="UTF-8"):
        ev.load_golden(path)


def test_a_blank_should_answer_cell_follows_the_document_column() -> None:
    rows = ev.parse_golden(golden("1,English,Q?,Doc,page 1,", "2,English,R?,(none),,"))
    assert [r.should_answer for r in rows] == [True, False]


# ── location matcher ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("slide 5", ev.Location("pages", 5, 5, None)),
        ("Slide 3-4", ev.Location("pages", 3, 4, None)),
        ("slide 3 \u2013 4", ev.Location("pages", 3, 4, None)),  # an en dash and spaces
        ("page 2", ev.Location("pages", 2, 2, None)),
        ("Pages 2-4", ev.Location("pages", 2, 4, None)),
        ("pages 7\u20149", ev.Location("pages", 7, 9, None)),  # an em dash
        ("heading: Unit 2 > Loops", ev.Location("heading", None, None, "Unit 2 > Loops")),
        ("Heading:  प्रकाश  ", ev.Location("heading", None, None, "प्रकाश")),
    ],
)
def test_parse_location(text: str, expected: ev.Location) -> None:
    assert ev.parse_location(text) == expected


@pytest.mark.parametrize("text", ["", "slide", "slide 0", "pages 4-2", "page x", "heading:", "p. 3", "chapter 2"])
def test_parse_location_refuses_what_it_cannot_read(text: str) -> None:
    with pytest.raises(ev.GoldenSetError):
        ev.parse_location(text)


def test_pages_and_slides_hit_when_the_chunk_range_overlaps() -> None:
    loc = ev.parse_location("pages 4-6")
    assert ev.location_matches(loc, page_start=3, page_end=4, heading_path=None)  # touches at 4
    assert ev.location_matches(loc, page_start=5, page_end=5, heading_path=None)  # inside
    assert ev.location_matches(loc, page_start=6, page_end=9, heading_path=None)  # touches at 6
    assert ev.location_matches(loc, page_start=1, page_end=20, heading_path=None)  # covers it
    assert not ev.location_matches(loc, page_start=1, page_end=3, heading_path=None)
    assert not ev.location_matches(loc, page_start=7, page_end=7, heading_path=None)
    assert ev.location_matches(ev.parse_location("slide 5"), page_start=5, page_end=None, heading_path=None)
    assert not ev.location_matches(ev.parse_location("slide 5"), page_start=None, page_end=None, heading_path=None)
    assert ev.location_matches(ev.parse_location("slide 5"), page_start=None, page_end=5, heading_path=None)


def test_a_heading_hits_on_a_casefolded_substring() -> None:
    loc = ev.parse_location("heading: loops")
    assert ev.location_matches(loc, page_start=1, page_end=1, heading_path="Unit 2 > LOOPS and Iteration")
    assert not ev.location_matches(loc, page_start=1, page_end=1, heading_path="Unit 2 > Functions")
    assert not ev.location_matches(loc, page_start=1, page_end=1, heading_path=None)
    assert ev.location_matches(ev.parse_location("heading: Unit 2  >   Loops"), page_start=None, page_end=None,
                               heading_path="Unit 2 > Loops")  # whitespace runs collapse


def test_a_devanagari_heading_matches_and_nfc_nfd_forms_are_equal() -> None:
    loc = ev.parse_location("heading: घटक २ > प्रकाशसंश्लेषण")
    assert ev.location_matches(loc, page_start=None, page_end=None, heading_path="पाठ ३ > घटक २ > प्रकाशसंश्लेषण क्रिया")
    assert not ev.location_matches(loc, page_start=None, page_end=None, heading_path="घटक २ > श्वसन")
    # NFC against NFD: Latin and the Devanagari nukta letter (U+0958 is a composition exclusion: NFC splits it)
    for expected, stored in (("Café", "Unit 1 > Café society"), ("क़िला", "विभाग > क़िला")):
        assert unicodedata.normalize("NFC", expected) != stored or expected == stored
        for form in ("NFC", "NFD"):
            loc = ev.parse_location("heading: " + unicodedata.normalize(form, expected))
            assert ev.location_matches(loc, page_start=None, page_end=None, heading_path=stored), (expected, form)
            loc = ev.parse_location("heading: " + expected)
            assert ev.location_matches(
                loc, page_start=None, page_end=None, heading_path=unicodedata.normalize(form, stored)
            ), (expected, form)


def test_the_document_matches_by_title_file_name_or_file_stem() -> None:
    kw = {"title": "Science Chapter 3", "original_filename": "ch3_science.PPTX"}
    assert ev.document_matches("Science Chapter 3", **kw)
    assert ev.document_matches("  science   chapter 3 ", **kw)  # case and spaces
    assert ev.document_matches("ch3_science.pptx", **kw)
    assert ev.document_matches("ch3_science", **kw)
    assert not ev.document_matches("Science", **kw), "no partial names: 'Science' would match every science file"
    assert not ev.document_matches("Maths Chapter 3", **kw)
    assert ev.document_matches("मराठी भाग १", title="मराठी भाग १", original_filename="mr-01.pptx")


# ── per-question results and aggregate metrics ───────────────────────────────


def chunk(
    cid: str, *, title: str = "Biology Notes", fname: str = "bio.pdf", pages: tuple[int | None, int | None] = (1, 1),
    heading: str | None = None, sim: float | None = None, kw: int | None = None, fused: float = 0.01,
) -> ev.ChunkRef:
    return ev.ChunkRef(
        chunk_id=cid, document_title=title, original_filename=fname, page_start=pages[0], page_end=pages[1],
        heading_path=heading, vector_similarity=sim, keyword_matched_terms=kw, fused_score=fused,
    )


def retrieved(
    hits: list[ev.ChunkRef], *, sufficient: bool, max_sim: float | None, vector: list[ev.ChunkRef] | None = None,
    keyword: list[ev.ChunkRef] | None = None, reason: str | None = None, candidates: int = 10,
    coverage: float | None = None,
) -> ev.Retrieved:
    return ev.Retrieved(
        hits=tuple(hits), vector_ranked=tuple(vector if vector is not None else hits),
        keyword_ranked=tuple(keyword if keyword is not None else ()), sufficient=sufficient,
        reason=reason or ("similarity" if sufficient else "below_threshold"), max_similarity=max_sim,
        keyword_coverage=coverage, candidate_count=candidates,
    )


def rows_from(csv_text: str) -> list[ev.GoldenRow]:
    return ev.parse_golden(csv_text)


SCOPE_DOCS = [ev.DocRef("Biology Notes", "bio.pdf"), ev.DocRef("Deck", "deck.pptx")]


def synthetic_results() -> list[ev.QuestionResult]:
    """Eight questions: English 1-4 (answer), Marathi 5-6 (answer), English 7 and Marathi 8 (refuse)."""
    rows = rows_from(golden(
        "1,English,Q1,Biology Notes,page 1,Yes",  # right chunk first, sufficient
        "2,English,Q2,Biology Notes,page 2,Yes",  # right chunk second, sufficient
        "3,English,Q3,Biology Notes,page 3,Yes",  # right chunk fifth, below threshold
        "4,English,Q4,Biology Notes,page 9,Yes",  # right chunk not retrieved: wrong chunk
        "5,Marathi,Q5,Biology Notes,heading: प्रकाश,Yes",  # right chunk first, sufficient
        "6,Marathi,Q6,Missing Doc,page 1,Yes",  # the document is not in scope
        "7,English,Q7,(none),,No",  # refused correctly
        "8,Marathi,Q8,(none),,No",  # answered anyway
    ))
    p1, p2, p3 = chunk("a1", pages=(1, 1)), chunk("a2", pages=(2, 2)), chunk("a3", pages=(3, 3))
    other = [chunk(f"o{i}", pages=(10 + i, 10 + i)) for i in range(5)]
    mr = chunk("m1", heading="घटक २ > प्रकाशसंश्लेषण")
    results = [
        ev.evaluate_question(rows[0], retrieved([p1, *other[:3]], sufficient=True, max_sim=0.90,
                                                vector=[p1, *other[:3]], keyword=[p1]), SCOPE_DOCS),
        ev.evaluate_question(rows[1], retrieved([other[0], p2, other[1]], sufficient=True, max_sim=0.88,
                                                vector=[other[0], p2], keyword=[other[0]]), SCOPE_DOCS),
        ev.evaluate_question(rows[2], retrieved([*other[:4], p3], sufficient=False, max_sim=0.78,
                                                vector=[*other[:4], p3], keyword=[]), SCOPE_DOCS),
        ev.evaluate_question(rows[3], retrieved(other[:3], sufficient=True, max_sim=0.86), SCOPE_DOCS),
        ev.evaluate_question(rows[4], retrieved([mr, other[0]], sufficient=True, max_sim=0.91,
                                                vector=[mr], keyword=[]), SCOPE_DOCS),
        ev.evaluate_question(rows[5], retrieved(other[:2], sufficient=True, max_sim=0.87), SCOPE_DOCS),
        ev.evaluate_question(rows[6], retrieved(other[:2], sufficient=False, max_sim=0.60), SCOPE_DOCS),
        ev.evaluate_question(rows[7], retrieved(other[:2], sufficient=True, max_sim=0.89), SCOPE_DOCS),
    ]
    return results


def test_each_question_gets_a_hit_position_and_a_failure_kind() -> None:
    r = synthetic_results()
    assert [x.hit_position for x in r] == [1, 2, 5, None, 1, None, None, None]
    assert [x.vector_hit_position for x in r] == [1, 2, 5, None, 1, None, None, None]
    assert [x.keyword_hit_position for x in r] == [1, None, None, None, None, None, None, None]
    assert [x.outcome for x in r] == [
        ev.OK, ev.OK, ev.BELOW_THRESHOLD, ev.WRONG_CHUNK, ev.OK, ev.DOCUMENT_NOT_IN_SCOPE,
        ev.REFUSED_CORRECTLY, ev.ANSWERED_ANYWAY,
    ]
    assert r[2].sufficient is False and r[0].sufficient is True


def test_a_question_with_no_hits_is_no_candidate() -> None:
    row = rows_from(golden("1,English,Q?,Biology Notes,page 1,Yes"))[0]
    empty = retrieved([], sufficient=False, max_sim=None, reason="no_candidates", candidates=0)
    assert ev.evaluate_question(row, empty, SCOPE_DOCS).outcome == ev.NO_CANDIDATE


def test_the_per_question_table_keeps_the_top_five_and_never_any_chunk_text() -> None:
    row = rows_from(golden("1,English,Q?,Biology Notes,page 2,Yes"))[0]
    hits = [chunk(f"c{i}", pages=(i, i), sim=0.9 - i / 100, kw=i, fused=1 / (60 + i)) for i in range(1, 8)]
    result = ev.evaluate_question(row, retrieved(hits, sufficient=True, max_sim=0.89), SCOPE_DOCS)
    top = result.top
    assert [t["rank"] for t in top] == [1, 2, 3, 4, 5]
    assert top[1]["is_match"] is True and top[0]["is_match"] is False
    assert set(top[0]) == {
        "rank", "chunk_id", "document_title", "page_start", "page_end", "heading_path", "vector_similarity",
        "keyword_matched_terms", "fused_score", "is_match",
    }
    assert "content" not in {f.name for f in ev.ChunkRef.__dataclass_fields__.values()}


def test_hit_at_k_mrr_answer_rate_and_refusal_accuracy() -> None:
    m = ev.aggregate(synthetic_results(), cutoff=10)
    # should-answer rows: 6 (Q1..Q6). Hits at 1, 2, 5, none, 1, none
    assert m["should_answer"] == 6 and m["should_not_answer"] == 2
    assert m["hit_at"] == {"1": 2 / 6, "3": 3 / 6, "5": 4 / 6, "10": 4 / 6}
    assert m["mrr"] == pytest.approx((1 + 1 / 2 + 1 / 5 + 0 + 1 + 0) / 6) and m["mrr_cutoff"] == 10
    assert m["refusal_accuracy"] == pytest.approx(1 / 2)  # Q7 refused, Q8 answered


def test_answer_rate_counts_sufficient_rows_whether_or_not_the_chunk_is_right() -> None:
    m = ev.aggregate(synthetic_results(), cutoff=10)
    # sufficient: Q1 Q2 Q4 Q5 Q6 -> 5 of 6 (Q3 is below the threshold)
    assert m["answer_rate"] == pytest.approx(5 / 6)


def test_mrr_uses_the_cutoff() -> None:
    m = ev.aggregate(synthetic_results(), cutoff=3)
    assert m["mrr"] == pytest.approx((1 + 1 / 2 + 0 + 0 + 1 + 0) / 6) and m["hit_at"]["3"] == 3 / 6
    assert "10" not in m["hit_at"] and m["hit_at"]["5"] == 4 / 6, "the cutoff adds a column only when it is new"


def test_keyword_only_and_vector_only_hit_at_k() -> None:
    m = ev.aggregate(synthetic_results(), cutoff=10)
    assert m["keyword_only_hit_at"] == {"1": 1 / 6, "3": 1 / 6, "5": 1 / 6, "10": 1 / 6}
    assert m["vector_only_hit_at"] == {"1": 2 / 6, "3": 3 / 6, "5": 4 / 6, "10": 4 / 6}


def test_the_failure_counts_name_what_went_wrong() -> None:
    m = ev.aggregate(synthetic_results(), cutoff=10)
    assert m["outcomes"] == {
        ev.OK: 3, ev.BELOW_THRESHOLD: 1, ev.WRONG_CHUNK: 1, ev.NO_CANDIDATE: 0, ev.DOCUMENT_NOT_IN_SCOPE: 1,
        ev.REFUSED_CORRECTLY: 1, ev.ANSWERED_ANYWAY: 1,
    }


def test_everything_is_split_by_language() -> None:
    by = ev.by_language(synthetic_results(), cutoff=10)
    assert list(by) == ["English", "Marathi"]
    en, mr = by["English"], by["Marathi"]
    assert (en["should_answer"], en["should_not_answer"]) == (4, 1)
    assert en["hit_at"]["1"] == 1 / 4 and en["hit_at"]["3"] == 2 / 4 and en["hit_at"]["5"] == 3 / 4
    assert en["answer_rate"] == pytest.approx(3 / 4) and en["refusal_accuracy"] == 1.0
    assert (mr["should_answer"], mr["should_not_answer"]) == (2, 1)
    assert mr["hit_at"]["1"] == 1 / 2 and mr["answer_rate"] == 1.0 and mr["refusal_accuracy"] == 0.0


def test_a_group_with_no_rows_of_a_kind_reports_none_not_zero() -> None:
    only_refusals = [r for r in synthetic_results() if not r.should_answer]
    m = ev.aggregate(only_refusals, cutoff=10)
    assert m["should_answer"] == 0 and m["hit_at"] is None and m["mrr"] is None and m["answer_rate"] is None
    only_answers = [r for r in synthetic_results() if r.should_answer]
    assert ev.aggregate(only_answers, cutoff=10)["refusal_accuracy"] is None


# ── threshold sweep ──────────────────────────────────────────────────────────


def sim_only(sim: float | None, *, answer: bool, coverage: float | None = None) -> ev.QuestionResult:
    row = rows_from(golden(
        "1,English,Q?,Biology Notes,page 1,Yes" if answer else "1,English,Q?,(none),,No"
    ))[0]
    hit = chunk("c", pages=(1, 1))
    return ev.evaluate_question(
        row, retrieved([hit] if sim is not None else [], sufficient=False, max_sim=sim, coverage=coverage), SCOPE_DOCS
    )


def sweep_inputs() -> list[ev.QuestionResult]:
    answers = [sim_only(s, answer=True) for s in (0.95, 0.90, 0.80, 0.75)]
    refusals = [sim_only(s, answer=False) for s in (0.72, 0.88)]
    return [*answers, *refusals]


def test_the_sweep_covers_070_to_095_in_steps_of_001_without_float_drift() -> None:
    assert len(ev.THRESHOLDS) == 26 and ev.THRESHOLDS[0] == 0.70 and ev.THRESHOLDS[-1] == 0.95
    assert ev.THRESHOLDS[5] == 0.75 and ev.THRESHOLDS[15] == 0.85, "the doubles equal the decimal literals"
    rows = ev.sweep(sweep_inputs())
    assert [r.threshold for r in rows] == list(ev.THRESHOLDS)


def test_sweep_rows_give_answer_rate_and_refusal_accuracy_per_threshold() -> None:
    by_t = {r.threshold: r for r in ev.sweep(sweep_inputs())}
    # answers 0.95 0.90 0.80 0.75; refusals 0.72 0.88; a question is answered when its similarity >= threshold
    for t, answered, refused in ((0.70, 4, 0), (0.73, 4, 1), (0.75, 4, 1), (0.76, 3, 1), (0.81, 2, 1),
                                 (0.88, 2, 1), (0.89, 2, 2), (0.90, 2, 2), (0.91, 1, 2), (0.95, 1, 2)):
        row = by_t[t]
        assert row.answer_rate == answered / 4, t
        assert row.refusal_accuracy == refused / 2, t
        assert row.balanced == pytest.approx((answered / 4 + refused / 2) / 2), t


def test_the_two_summary_thresholds() -> None:
    summary = ev.summarize_sweep(ev.sweep(sweep_inputs()))
    # balanced accuracy is 0.75 at 0.73-0.75 and at 0.89-0.90: the highest of the tied thresholds is reported
    assert summary.best_balanced is not None and summary.best_balanced.threshold == 0.90
    assert summary.best_balanced.balanced == pytest.approx(0.75)
    # answer rate stays at or above 90% up to 0.75 (4 of 4), and drops to 3 of 4 at 0.76
    assert summary.highest_keeping_answer_rate is not None
    assert summary.highest_keeping_answer_rate.threshold == 0.75
    assert summary.min_answer_rate == 0.90


def test_the_summary_thresholds_are_none_when_they_cannot_be_decided() -> None:
    only_answers = ev.summarize_sweep(ev.sweep([sim_only(0.9, answer=True)]))
    assert only_answers.best_balanced is None, "balanced accuracy needs rows of both kinds"
    assert only_answers.highest_keeping_answer_rate is not None
    only_refusals = ev.summarize_sweep(ev.sweep([sim_only(0.9, answer=False)]))
    assert only_refusals.best_balanced is None and only_refusals.highest_keeping_answer_rate is None
    low = ev.summarize_sweep(ev.sweep([sim_only(0.5, answer=True), sim_only(0.5, answer=False)]))
    assert low.highest_keeping_answer_rate is None, "no threshold in 0.70-0.95 keeps 90% of answers"


def test_a_question_with_no_similarity_is_never_answered() -> None:
    rows = ev.sweep([sim_only(None, answer=True), sim_only(None, answer=False)])
    assert all(r.answer_rate == 0.0 and r.refusal_accuracy == 1.0 for r in rows)


def test_the_keyword_coverage_sweep_uses_the_stored_coverage() -> None:
    results = [sim_only(0.5, answer=True, coverage=0.9), sim_only(0.5, answer=True, coverage=0.7),
               sim_only(0.5, answer=False, coverage=0.75), sim_only(0.5, answer=False, coverage=None)]
    by_t = {r.threshold: r for r in ev.sweep(results, signal="keyword_coverage")}
    assert (by_t[0.70].answer_rate, by_t[0.70].refusal_accuracy) == (1.0, 0.5)
    assert (by_t[0.80].answer_rate, by_t[0.80].refusal_accuracy) == (0.5, 1.0)
    with pytest.raises(ValueError):
        ev.sweep(results, signal="vibes")


# ── report and summary text ──────────────────────────────────────────────────


def test_the_report_is_plain_json_with_every_section() -> None:
    meta = {"tenant": "acme", "model": "m", "threshold": 0.85, "threshold_calibrated": False, "k": 10,
            "corpus": {"documents": [{"title": "Biology Notes", "original_filename": "bio.pdf", "chunks": 7, "flagged": 1}],
                       "chunks": 7, "flagged_chunks": 1}}
    report = ev.build_report(synthetic_results(), cutoff=10, meta=meta)
    again = json.loads(json.dumps(report, ensure_ascii=False))
    assert again == report
    assert set(report) >= {"meta", "overall", "by_language", "questions", "sweep", "keyword_coverage_sweep"}
    assert report["overall"]["should_answer"] == 6
    assert len(report["questions"]) == 8 and report["questions"][0]["number"] == "1"
    assert report["sweep"]["rows"][0]["threshold"] == 0.70 and len(report["sweep"]["rows"]) == 26
    assert report["sweep"]["best_balanced"]["threshold"] is not None
    assert report["questions"][2]["outcome"] == ev.BELOW_THRESHOLD and report["questions"][2]["max_similarity"] == 0.78


def test_the_console_summary_says_what_failed_and_why_in_plain_english() -> None:
    meta = {"tenant": "acme", "class_id": "c", "subject_id": "s", "model": "e5", "threshold": 0.85,
            "threshold_calibrated": False, "k": 10, "keyword_max_candidates": 5,
            "corpus": {"documents": [{"title": "Biology Notes", "original_filename": "bio.pdf", "chunks": 7, "flagged": 1}],
                       "chunks": 7, "flagged_chunks": 1}}
    text = ev.format_console(ev.build_report(synthetic_results(), cutoff=10, meta=meta))
    for phrase in (
        "English", "Marathi", "hit@1", "MRR@10", "answer rate", "refusal accuracy",
        "found but below the threshold", "wrong chunk", "document is not in this class and subject",
        "answered although it should have refused", "best balanced threshold", "highest threshold",
        "Biology Notes", "1 flagged", "Keyword only", "vector only",
        "Keyword cap (backlog B97)", "this scope has 7, so the cap CAN drop a better match",
    ):
        assert phrase in text, phrase
    assert "Q3" in text and "0.78" in text, "the failed question and its similarity are named"
    assert "0.85" in text and "not calibrated" in text.lower()


def test_console_text_drops_control_characters_from_untrusted_titles() -> None:
    assert ev.safe_text("Title\x1b[31m red\nline\u202e", 40) == "Title [31m red line"
    assert ev.safe_text("x" * 100, 10) == "xxxxxxx..."
