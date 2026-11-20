"""
tests/test_retrieval_calibration.py
───────────────────────────────────
Step 5d-5 (final retrieval calibration), the parts that need no database:

  * the new defaults: the keyword side is OFF (RETRIEVAL_RRF_KEYWORD_WEIGHT 0.0, vector only), the threshold is
    0.84, and RETRIEVAL_CALIBRATED_MODEL_KEY names the model it was measured for;
  * the calibration record (date, model key, threshold, golden-set size) agrees with the defaults;
  * threshold_calibrated is DERIVED from the active model key, never a constant;
  * the keyword side runs only with a keyword weight above 0, and a keyword-coverage clause without a keyword side
    is refused (it would silently never fire);
  * the startup log line (threshold, calibrated model key, active model key, match);
  * the harness wording: "calibrated on <date> for <model key> using N / M questions", or a warning;
  * prove-it: with threshold_calibrated forced to a constant true, the false-case check FAILS.
"""

import dataclasses
from typing import Any

import pytest

from app import main
from app.core.config import Settings
from app.ml.model_keys import E5_SMALL_MODEL_KEY
from app.services import retrieval
from app.services import retrieval_eval as ev
from app.services.retrieval import RetrievalConfig

CALIBRATED_KEY = "intfloat/multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3"
OTHER_KEY = "BAAI/bge-m3@0000000000000000000000000000000000000000"


def fresh() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ── defaults ─────────────────────────────────────────────────────────────────


def test_the_settings_defaults_are_vector_only_at_the_calibrated_threshold() -> None:
    s = fresh()
    assert s.RETRIEVAL_RRF_KEYWORD_WEIGHT == 0.0, "the keyword side is switched off by default (5d-5)"
    assert s.RETRIEVAL_RRF_VECTOR_WEIGHT == 1.0
    assert s.RETRIEVAL_KEYWORD_MAX_DF == 1.0, "pruning stays off"
    assert s.RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS == 1
    assert s.RETRIEVAL_MIN_SIMILARITY == 0.84
    assert s.RETRIEVAL_CALIBRATED_MODEL_KEY == CALIBRATED_KEY == E5_SMALL_MODEL_KEY


def test_the_config_defaults_equal_the_settings_defaults() -> None:
    cfg = RetrievalConfig()
    assert (cfg.rrf_vector_weight, cfg.rrf_keyword_weight, cfg.keyword_max_df, cfg.keyword_min_matched_terms) == (
        1.0, 0.0, 1.0, 1,
    )
    assert cfg.min_similarity == 0.84 and cfg.calibrated_model_key == CALIBRATED_KEY
    assert RetrievalConfig.from_settings(fresh()) == cfg


def test_the_calibration_setting_reaches_the_config() -> None:
    s = Settings(_env_file=None, RETRIEVAL_CALIBRATED_MODEL_KEY=OTHER_KEY)  # type: ignore[call-arg]
    assert RetrievalConfig.from_settings(s).calibrated_model_key == OTHER_KEY


def test_the_calibration_record_matches_the_defaults_and_the_measured_set() -> None:
    record = retrieval.CALIBRATION
    assert record.model_key == fresh().RETRIEVAL_CALIBRATED_MODEL_KEY
    assert record.threshold == fresh().RETRIEVAL_MIN_SIMILARITY == 0.84
    assert (record.should_answer, record.should_not_answer) == (30, 10)
    assert (record.documents, record.chunks) == (2, 100)
    assert record.date == "2026-11-20"


# ── threshold_calibrated is derived ──────────────────────────────────────────


def calibrated_for(model_key: str, cfg: RetrievalConfig | None = None) -> bool:
    """What a search result reports for this model (through _result, the one place results are built)."""
    return retrieval._result(cfg or RetrievalConfig(), model_key, has_scope=False).threshold_calibrated


def check_false_cases() -> None:
    """The check the prove-it test runs with the derivation replaced by a constant."""
    assert calibrated_for(OTHER_KEY) is False, "another model: the threshold was not measured for it"
    assert calibrated_for("fake-hash-384") is False
    assert calibrated_for(CALIBRATED_KEY.upper()) is False, "the key is compared exactly"
    assert calibrated_for(CALIBRATED_KEY, RetrievalConfig(calibrated_model_key="")) is False, "no calibrated model"
    assert calibrated_for(CALIBRATED_KEY, RetrievalConfig(calibrated_model_key=OTHER_KEY)) is False


def test_threshold_calibrated_is_true_only_for_the_calibrated_model_key() -> None:
    assert calibrated_for(CALIBRATED_KEY) is True
    assert calibrated_for(OTHER_KEY, RetrievalConfig(calibrated_model_key=OTHER_KEY)) is True
    check_false_cases()
    assert retrieval.is_threshold_calibrated(CALIBRATED_KEY, RetrievalConfig()) is True
    assert retrieval.is_threshold_calibrated(OTHER_KEY, RetrievalConfig()) is False


def test_threshold_calibrated_is_no_longer_a_config_field() -> None:
    assert "threshold_calibrated" not in {f.name for f in dataclasses.fields(RetrievalConfig)}


def test_prove_it_a_constant_true_fails_the_false_case_check(monkeypatch: pytest.MonkeyPatch) -> None:
    check_false_cases()  # derived: passes
    monkeypatch.setattr(retrieval, "is_threshold_calibrated", lambda model_key, config: True)
    with pytest.raises(AssertionError):
        check_false_cases()


# ── the keyword side runs only when it has a say ─────────────────────────────


def test_the_keyword_side_runs_only_with_a_keyword_weight_above_zero() -> None:
    assert retrieval.keyword_side_runs(RetrievalConfig()) is False, "default: vector only"
    assert retrieval.keyword_side_runs(RetrievalConfig(rrf_keyword_weight=1.0)) is True
    assert retrieval.keyword_side_runs(RetrievalConfig(rrf_keyword_weight=0.01)) is True


def test_a_keyword_coverage_clause_without_a_keyword_side_is_refused() -> None:
    """With the keyword side off nothing measures coverage, so the clause could never fire: refuse it loudly."""
    from pydantic import ValidationError

    with pytest.raises(ValueError, match="min_keyword_coverage"):
        RetrievalConfig(min_keyword_coverage=0.6)
    assert RetrievalConfig(min_keyword_coverage=0.6, rrf_keyword_weight=1.0).min_keyword_coverage == 0.6
    with pytest.raises(ValidationError, match="RETRIEVAL_MIN_KEYWORD_COVERAGE"):
        Settings(_env_file=None, RETRIEVAL_MIN_KEYWORD_COVERAGE=0.6)  # type: ignore[call-arg]
    ok = Settings(  # type: ignore[call-arg]
        _env_file=None, RETRIEVAL_MIN_KEYWORD_COVERAGE=0.6, RETRIEVAL_RRF_KEYWORD_WEIGHT=1.0
    )
    assert RetrievalConfig.from_settings(ok).min_keyword_coverage == 0.6


# ── the startup log line ─────────────────────────────────────────────────────


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append(("info", event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append(("warning", event, kw))


def test_the_startup_line_states_the_threshold_the_calibrated_key_and_the_match(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    monkeypatch.setattr(retrieval, "logger", recorder)
    assert retrieval.log_threshold_calibration(fresh()) is True
    level, event, fields = recorder.events[-1]
    assert (level, event) == ("info", "retrieval_threshold")
    assert fields == {
        "threshold": 0.84, "calibrated_model_key": CALIBRATED_KEY, "active_model_key": CALIBRATED_KEY, "calibrated": True,
    }

    other = Settings(_env_file=None, RETRIEVAL_CALIBRATED_MODEL_KEY=OTHER_KEY)  # type: ignore[call-arg]
    assert retrieval.log_threshold_calibration(other) is False
    level, event, fields = recorder.events[-1]
    assert (level, event) == ("warning", "retrieval_threshold") and fields["calibrated"] is False
    assert fields["active_model_key"] == CALIBRATED_KEY and fields["calibrated_model_key"] == OTHER_KEY

    unpinned = Settings(_env_file=None, EMBEDDING_MODEL_ID="bge-m3")  # type: ignore[call-arg]
    assert retrieval.log_threshold_calibration(unpinned) is False, "a model without a pinned key is never calibrated"
    assert recorder.events[-1][2]["active_model_key"] is None


async def test_the_app_logs_the_threshold_line_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def no_redis() -> None:
        return None

    monkeypatch.setattr(main, "get_redis", no_redis)
    monkeypatch.setattr(main, "close_redis", no_redis)
    monkeypatch.setattr(retrieval, "log_threshold_calibration", lambda *a: calls.append(a) or True)
    async with main.lifespan(main.app):
        assert len(calls) == 1


# ── the harness wording ──────────────────────────────────────────────────────


def meta_for(model: str, *, calibrated: bool, threshold: float = 0.84) -> dict[str, Any]:
    return {
        "tenant": "acme", "class_id": "c", "subject_id": "s", "model": model, "k": 10, "threshold": threshold,
        "threshold_calibrated": calibrated, "calibrated_model_key": CALIBRATED_KEY,
        "calibration": dataclasses.asdict(retrieval.CALIBRATION) if model == CALIBRATED_KEY else None,
    }


def test_the_note_names_date_model_and_set_size_when_the_model_matches() -> None:
    note = ev.calibration_note(meta_for(CALIBRATED_KEY, calibrated=True))
    assert note == (
        f"calibrated on 2026-11-20 for {CALIBRATED_KEY} using 30 should-answer / 10 should-not-answer questions"
    )
    assert "not calibrated yet" not in note


def test_the_note_says_when_the_threshold_differs_from_the_measured_value() -> None:
    note = ev.calibration_note(meta_for(CALIBRATED_KEY, calibrated=True, threshold=0.86))
    assert note.startswith("calibrated on 2026-11-20") and "measured value was 0.84" in note and "0.86" in note


def test_the_note_warns_when_the_model_is_not_the_calibrated_one() -> None:
    note = ev.calibration_note(meta_for(OTHER_KEY, calibrated=False))
    assert "not calibrated for this model" in note and CALIBRATED_KEY in note


def test_both_reports_print_the_record_line_or_the_warning() -> None:
    result = ev.QuestionResult(
        number="1", row=2, language="English", question="q", document="D", expected_location="page 1",
        should_answer=True, sufficient=True, reason="similarity", max_similarity=0.9, keyword_coverage=None,
        candidate_count=1, hit_position=1, vector_hit_position=1, keyword_hit_position=None, outcome=ev.OK, top=(),
    )
    for model, calibrated in ((CALIBRATED_KEY, True), (OTHER_KEY, False)):
        meta = meta_for(model, calibrated=calibrated)
        console = ev.format_console(ev.build_report([result], cutoff=10, meta=meta))
        run = ev.ConfigRun("current", "current settings", ev.vector_only_point(), (result,))
        compare = ev.format_compare(ev.build_compare_report([run], cutoff=10, meta=meta))
        for text in (console, compare):
            assert "not calibrated yet: a default, not a measured value" not in text
            if calibrated:
                assert "calibrated on 2026-11-20 for" in text and "WARNING: the threshold is not calibrated" not in text
            else:
                assert "WARNING: the threshold is not calibrated for this model" in text


def test_the_normal_report_marks_the_keyword_numbers_as_not_measured_when_the_side_is_off() -> None:
    result = ev.QuestionResult(
        number="1", row=2, language="English", question="q", document="D", expected_location="page 1",
        should_answer=True, sufficient=True, reason="similarity", max_similarity=0.9, keyword_coverage=None,
        candidate_count=1, hit_position=1, vector_hit_position=1, keyword_hit_position=None, outcome=ev.OK, top=(),
    )
    meta = {**meta_for(CALIBRATED_KEY, calibrated=True), "keyword_side": "off",
            "fusion": {"keyword_max_df": 1.0, "rrf_vector_weight": 1.0, "rrf_keyword_weight": 0.0,
                       "keyword_min_matched_terms": 1}}
    report = ev.build_report([result], cutoff=10, meta=meta)
    assert report["overall"]["keyword_only_hit_at"] is None, "not measured is not 0%"
    assert all(m["keyword_only_hit_at"] is None for m in report["by_language"].values())
    assert report["overall"]["vector_only_hit_at"]["1"] == 1.0
    text = ev.format_console(report)
    assert "keyword side is switched off" in text
