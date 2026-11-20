"""
tests/test_retrieval_fusion_pure.py
───────────────────────────────────
Step 5d-4, the pure parts: weighted reciprocal rank fusion, the frequency-pruning decision for the keyword
query, and the new settings. No database, no model. Every new setting defaults to TODAY'S behaviour.
"""

import uuid

import pytest

from app.core.config import Settings
from app.services import retrieval
from app.services.retrieval import RetrievalConfig


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i + 1) for i in range(n)]


# ── weighted RRF ─────────────────────────────────────────────────────────────


def test_weighted_rrf_score_is_the_sum_of_weight_over_rank_constant_plus_rank() -> None:
    a, b, c = _ids(3)
    fused = retrieval.rrf_fuse([a, b], [b, c], 60, vector_weight=3.0, keyword_weight=0.5)
    scores = {f.id: f.score for f in fused}
    assert scores[a] == pytest.approx(3.0 / 61)
    assert scores[b] == pytest.approx(3.0 / 62 + 0.5 / 61)
    assert scores[c] == pytest.approx(0.5 / 62)


def test_default_weights_give_exactly_todays_scores() -> None:
    """Not approximately: the same floating-point numbers as the unweighted formula 1/(k+v) + 1/(k+kw)."""
    ids = _ids(6)
    vector, keyword = ids[:4], [ids[3], ids[0], ids[5]]
    default = retrieval.rrf_fuse(vector, keyword, 60)
    explicit = retrieval.rrf_fuse(vector, keyword, 60, vector_weight=1.0, keyword_weight=1.0)
    assert default == explicit
    for f in default:
        v, k = f.vector_rank, f.keyword_rank
        today = (1.0 / (60 + v) if v is not None else 0.0) + (1.0 / (60 + k) if k is not None else 0.0)
        assert f.score == today, (f.id, f.score, today)


def test_a_zero_keyword_weight_equals_the_vector_side_alone() -> None:
    ids = _ids(6)
    vector, keyword = [ids[2], ids[0], ids[4]], [ids[5], ids[0], ids[1]]
    fused = retrieval.rrf_fuse(vector, keyword, 60, keyword_weight=0.0)
    alone = retrieval.rrf_fuse(vector, [], 60)
    assert [(f.id, f.score) for f in fused] == [(f.id, f.score) for f in alone]
    assert [f.id for f in fused] == vector, "chunks that only the zero-weight side lists are not in the result"


def test_a_zero_vector_weight_equals_the_keyword_side_alone() -> None:
    ids = _ids(6)
    vector, keyword = [ids[2], ids[0], ids[4]], [ids[5], ids[0], ids[1]]
    fused = retrieval.rrf_fuse(vector, keyword, 60, vector_weight=0.0)
    alone = retrieval.rrf_fuse([], keyword, 60)
    assert [(f.id, f.score) for f in fused] == [(f.id, f.score) for f in alone]
    assert [f.id for f in fused] == keyword


def test_weights_change_the_order() -> None:
    """a is first on the vector side only; b is second on the vector side and first on the keyword side.
    Equal weights: b wins (it has both). A keyword side that counts for almost nothing: a wins."""
    a, b, c = _ids(3)
    vector, keyword = [a, b], [b, c]
    assert retrieval.rrf_fuse(vector, keyword, 60)[0].id == b
    assert retrieval.rrf_fuse(vector, keyword, 60, keyword_weight=0.01)[0].id == a
    assert retrieval.rrf_fuse(vector, keyword, 60, vector_weight=0.01)[0].id == b
    assert retrieval.rrf_fuse(vector, keyword, 60, vector_weight=100.0)[0].id == a, "100 / 61 beats 100 / 62 + 1 / 61"


@pytest.mark.parametrize("weights", [(-1.0, 1.0), (1.0, -0.1), (float("nan"), 1.0), (1.0, float("inf"))])
def test_a_negative_or_non_finite_weight_is_refused(weights: tuple[float, float]) -> None:
    with pytest.raises(ValueError, match="weight"):
        retrieval.rrf_fuse(_ids(2), _ids(2), 60, vector_weight=weights[0], keyword_weight=weights[1])


def test_select_hits_survives_a_fusion_without_the_top_vector_chunk() -> None:
    """With a zero vector weight the top vector chunk may not be in the fused list; it must not crash."""
    a, b, c = _ids(3)
    vector, keyword = [a, b], [c]
    fused = retrieval.rrf_fuse(vector, keyword, 60, vector_weight=0.0)
    assert [f.id for f in retrieval.select_hits(fused, vector, 2)] == [c]


# ── frequency pruning ────────────────────────────────────────────────────────


def test_pruning_drops_the_lexemes_in_more_than_max_df_of_the_candidates() -> None:
    lexemes = ["what", "is", "osmosis", "err_x9"]
    doc_freq = [90, 80, 5, 1]  # of 100 candidate chunks
    assert retrieval.prune_lexemes(lexemes, doc_freq, 100, 0.5) == ["osmosis", "err_x9"]
    assert retrieval.prune_lexemes(lexemes, doc_freq, 100, 0.85) == ["is", "osmosis", "err_x9"]
    assert retrieval.prune_lexemes(lexemes, doc_freq, 100, 0.03) == ["err_x9"], "a rare identifier survives"


def test_pruning_keeps_a_lexeme_at_exactly_max_df() -> None:
    """'more than' max_df: 3 of 10 is exactly 0.3 and stays; 4 of 10 goes."""
    assert retrieval.prune_lexemes(["a", "b"], [3, 4], 10, 0.3) == ["a"]


def test_pruning_is_off_at_one_and_keeps_every_lexeme_even_one_in_every_chunk() -> None:
    assert retrieval.prune_lexemes(["a", "b"], [10, 10], 10, 1.0) == ["a", "b"]
    assert retrieval.prune_lexemes(["a", "b"], None, 10, 1.0) == ["a", "b"], "off needs no frequencies"


def test_pruning_keeps_a_lexeme_that_matches_no_candidate() -> None:
    assert retrieval.prune_lexemes(["ghost"], [0], 10, 0.0) == ["ghost"]


def test_pruning_needs_one_frequency_per_lexeme_when_it_is_on() -> None:
    with pytest.raises(ValueError):
        retrieval.prune_lexemes(["a", "b"], [1], 10, 0.5)
    with pytest.raises(ValueError):
        retrieval.prune_lexemes(["a"], None, 10, 0.5)


def test_pruning_is_language_independent() -> None:
    """No stopword list: Marathi and English function words go the same way, by frequency alone."""
    lexemes = ["ची", "आहेत", "प्रकाशसंश्लेषण", "what", "photosynthesis"]
    doc_freq = [95, 90, 2, 97, 2]
    assert retrieval.prune_lexemes(lexemes, doc_freq, 100, 0.5) == ["प्रकाशसंश्लेषण", "photosynthesis"]


def test_the_keyword_side_is_skipped_when_every_lexeme_is_pruned() -> None:
    config = RetrievalConfig(keyword_max_df=0.5)
    assert retrieval.keyword_lexemes(["the", "is"], [9, 8], 10, config) == []
    assert retrieval.keyword_lexemes(["the", "zebra"], [9, 1], 10, config) == ["zebra"]


def test_min_matched_terms_above_the_surviving_lexemes_skips_the_keyword_side() -> None:
    """No chunk can match 2 of 1 lexemes, so there is nothing to rank."""
    config = RetrievalConfig(keyword_max_df=0.5, keyword_min_matched_terms=2)
    assert retrieval.keyword_lexemes(["the", "zebra"], [9, 1], 10, config) == []
    assert retrieval.keyword_lexemes(["zebra", "gnu"], [1, 2], 10, config) == ["zebra", "gnu"]


def test_default_config_keeps_every_lexeme_without_frequencies() -> None:
    assert retrieval.keyword_lexemes(["a", "b", "c"], None, 10, RetrievalConfig()) == ["a", "b", "c"]


# ── settings and config: the defaults are neutral ────────────────────────────


def test_the_defaults_are_vector_only_since_5d5() -> None:
    """Changed deliberately in 5d-5: the keyword weight default went from 1.0 (hybrid) to 0.0 (vector only); the
    other three stay neutral. Explicit 1.0 / 1.0 still give the old hybrid (tests/test_retrieval_pruning.py)."""
    cfg = RetrievalConfig()
    assert (cfg.keyword_max_df, cfg.rrf_vector_weight, cfg.rrf_keyword_weight, cfg.keyword_min_matched_terms) == (
        1.0, 1.0, 0.0, 1,
    )
    fresh = Settings(_env_file=None)  # type: ignore[call-arg]
    assert fresh.RETRIEVAL_KEYWORD_MAX_DF == 1.0
    assert fresh.RETRIEVAL_RRF_VECTOR_WEIGHT == 1.0
    assert fresh.RETRIEVAL_RRF_KEYWORD_WEIGHT == 0.0
    assert fresh.RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS == 1
    assert fresh.RETRIEVAL_MIN_SIMILARITY == 0.84, "calibrated in 5d-5"
    assert RetrievalConfig.from_settings(fresh) == RetrievalConfig()


def test_the_config_validates_the_new_values() -> None:
    for bad in (-0.1, 1.1, float("nan")):
        with pytest.raises(ValueError, match="keyword_max_df"):
            RetrievalConfig(keyword_max_df=bad)
    with pytest.raises(ValueError, match="rrf_vector_weight"):
        RetrievalConfig(rrf_vector_weight=0.0)  # sufficiency reads the vector side: it must keep a say
    with pytest.raises(ValueError, match="rrf_keyword_weight"):
        RetrievalConfig(rrf_keyword_weight=-1.0)
    with pytest.raises(ValueError, match="keyword_min_matched_terms"):
        RetrievalConfig(keyword_min_matched_terms=0)
    assert RetrievalConfig(rrf_keyword_weight=0.0).rrf_keyword_weight == 0.0  # the vector-only baseline


def test_the_settings_validate_the_new_values() -> None:
    from pydantic import ValidationError

    for name, value in (
        ("RETRIEVAL_KEYWORD_MAX_DF", 1.5), ("RETRIEVAL_KEYWORD_MAX_DF", -0.1),
        ("RETRIEVAL_RRF_VECTOR_WEIGHT", 0.0), ("RETRIEVAL_RRF_KEYWORD_WEIGHT", -1.0),
        ("RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS", 0),
    ):
        with pytest.raises(ValidationError, match=name):
            Settings(_env_file=None, **{name: value})  # type: ignore[call-arg]
    ok = Settings(  # type: ignore[call-arg]
        _env_file=None, RETRIEVAL_KEYWORD_MAX_DF=0.3, RETRIEVAL_RRF_VECTOR_WEIGHT=2.0,
        RETRIEVAL_RRF_KEYWORD_WEIGHT=0.5, RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS=2,
    )
    cfg = RetrievalConfig.from_settings(ok)
    assert (cfg.keyword_max_df, cfg.rrf_vector_weight, cfg.rrf_keyword_weight, cfg.keyword_min_matched_terms) == (
        0.3, 2.0, 0.5, 2,
    )
