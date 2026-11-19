"""
tests/test_retrieval_pure.py
────────────────────────────
The pure parts of the retrieval service (step 5d-1): query normalisation, the OR tsquery text, RRF fusion
and the sufficiency decision. No database, no Redis, no model.
"""

import ast
import dataclasses
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import retrieval
from app.services.retrieval import QueryError, RetrievalConfig

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i + 1) for i in range(n)]


# ── normalize_query ──────────────────────────────────────────────────────────


def test_normalize_query_applies_nfc_drops_invisible_characters_and_collapses_spaces() -> None:
    # e + combining acute (NFD), zero width space, tab, newline, bidi override; Devanagari conjunct kept
    raw = "école​  what\tis\n‮osmosis क्षेत्र "
    assert retrieval.normalize_query(raw, 500) == "école what is osmosis क्षेत्र"


@pytest.mark.parametrize("raw", ["", "   ", "​‎﻿", "\t\n"])
def test_empty_query_after_cleaning_is_rejected(raw: str) -> None:
    with pytest.raises(QueryError) as exc:
        retrieval.normalize_query(raw, 500)
    assert exc.value.code == "invalid_query"


def test_query_length_is_measured_after_cleaning() -> None:
    assert retrieval.normalize_query("a" * 10 + " " * 50, 10) == "a" * 10
    with pytest.raises(QueryError) as exc:
        retrieval.normalize_query("a" * 11, 10)
    assert exc.value.code == "query_too_long"


def test_a_huge_raw_query_is_refused_without_cleaning_it_all() -> None:
    with pytest.raises(QueryError) as exc:
        retrieval.normalize_query("x " * 1_000_000, 500)
    assert exc.value.code == "query_too_long"


def test_normalize_query_refuses_non_strings() -> None:
    with pytest.raises(QueryError):
        retrieval.normalize_query(b"bytes", 500)  # type: ignore[arg-type]


# ── or_tsquery_text ──────────────────────────────────────────────────────────


def test_or_tsquery_joins_quoted_lexemes_with_or() -> None:
    assert retrieval.or_tsquery_text(["osmosis", "प्रकाशसंश्लेषण"]) == "'osmosis' | 'प्रकाशसंश्लेषण'"


def test_or_tsquery_escapes_quotes_and_backslashes() -> None:
    assert retrieval.or_tsquery_text(["it's"]) == "'it''s'"
    assert retrieval.or_tsquery_text(["a\\b"]) == "'a\\\\b'"
    assert retrieval.or_tsquery_text(["/a'b\\'c"]) == "'/a''b\\\\''c'"


@pytest.mark.parametrize("op", ["&", "|", "!", "(", ")", ":", "*", "<->", "<2>", ":*", "&&"])
def test_tsquery_operators_stay_inside_the_quoted_lexeme(op: str) -> None:
    text = retrieval.or_tsquery_text([f"x{op}y"])
    assert text == f"'x{op}y'"


def test_or_tsquery_drops_empty_lexemes_deduplicates_and_caps() -> None:
    assert retrieval.or_tsquery_text([]) is None
    assert retrieval.or_tsquery_text(["", ""]) is None
    assert retrieval.or_tsquery_text(["b", "a", "b"]) == "'b' | 'a'"
    many = [f"w{i:03d}" for i in range(retrieval.MAX_QUERY_LEXEMES + 10)]
    text = retrieval.or_tsquery_text(many)
    assert text is not None and text.count("|") == retrieval.MAX_QUERY_LEXEMES - 1


# ── rrf_fuse ─────────────────────────────────────────────────────────────────


def test_rrf_scores_are_the_sum_of_reciprocal_ranks() -> None:
    a, b, c = _ids(3)
    fused = retrieval.rrf_fuse([a, b], [b, c], 60)
    scores = {f.id: f.score for f in fused}
    assert scores[a] == pytest.approx(1 / 61)
    assert scores[b] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[c] == pytest.approx(1 / 62)
    assert [f.id for f in fused] == [b, a, c]
    by_id = {f.id: f for f in fused}
    assert (by_id[b].vector_rank, by_id[b].keyword_rank) == (2, 1)
    assert (by_id[a].vector_rank, by_id[a].keyword_rank) == (1, None)
    assert (by_id[c].vector_rank, by_id[c].keyword_rank) == (None, 2)


def test_rrf_ties_break_on_best_single_rank_then_id() -> None:
    a, b, c, d = _ids(4)
    # a: vector 1 only; d: keyword 1 only -> equal scores, equal best rank, so the smaller id wins
    fused = retrieval.rrf_fuse([a, b], [d, c], 60)
    assert [f.id for f in fused] == [a, d, b, c]
    assert retrieval.rrf_fuse([a, b], [d, c], 60) == fused  # deterministic
    assert retrieval.rrf_fuse([], [], 60) == []


def test_rrf_beats_each_side_when_a_chunk_is_second_on_both() -> None:
    target, vec_only, kw_only, f1, f2 = _ids(5)
    fused = retrieval.rrf_fuse([vec_only, target, f1, f2, kw_only], [kw_only, target], 60)
    assert fused[0].id == target


def test_rrf_ignores_duplicates_within_one_list() -> None:
    a, b = _ids(2)
    fused = retrieval.rrf_fuse([a, a, b], [], 60)
    assert [(f.id, f.vector_rank) for f in fused] == [(a, 1), (b, 2)]


# ── decide_sufficiency ───────────────────────────────────────────────────────

CONFIG = RetrievalConfig(min_similarity=0.85, min_keyword_coverage=0.0)


def test_sufficiency_reasons() -> None:
    def decide(**kw: object) -> tuple[bool, str]:
        args: dict[str, object] = {
            "has_scope": True, "candidate_count": 5, "max_similarity": 0.9, "keyword_coverage": None, "config": CONFIG,
        }
        args.update(kw)
        return retrieval.decide_sufficiency(**args)  # type: ignore[arg-type]

    assert decide(has_scope=False) == (False, "empty_scope")
    assert decide(candidate_count=0) == (False, "no_candidates")
    assert decide(max_similarity=0.85) == (True, "similarity")
    assert decide(max_similarity=0.8499) == (False, "below_threshold")
    assert decide(max_similarity=None) == (False, "below_threshold")
    # The keyword clause is OFF by default: full coverage alone is not enough
    assert decide(max_similarity=0.1, keyword_coverage=1.0) == (False, "below_threshold")
    on = dataclasses.replace(CONFIG, min_keyword_coverage=0.6)
    assert decide(max_similarity=0.1, keyword_coverage=0.6, config=on) == (True, "keyword_coverage")
    assert decide(max_similarity=0.1, keyword_coverage=0.59, config=on) == (False, "below_threshold")
    assert decide(max_similarity=0.9, keyword_coverage=1.0, config=on) == (True, "similarity")


# ── settings and config ──────────────────────────────────────────────────────


def test_config_comes_from_settings_with_the_planned_defaults() -> None:
    cfg = RetrievalConfig.from_settings()
    assert cfg.vector_k == settings.RETRIEVAL_VECTOR_K == 20
    assert cfg.keyword_k == settings.RETRIEVAL_KEYWORD_K == 20
    assert cfg.final_k == settings.RETRIEVAL_FINAL_K == 6
    assert cfg.rrf_k == settings.RETRIEVAL_RRF_K == 60
    assert cfg.max_query_chars == settings.RETRIEVAL_MAX_QUERY_CHARS == 500
    assert cfg.min_similarity == settings.RETRIEVAL_MIN_SIMILARITY == 0.85
    assert cfg.min_keyword_coverage == settings.RETRIEVAL_MIN_KEYWORD_COVERAGE == 0.0
    assert cfg.exact_max_candidates == settings.RETRIEVAL_EXACT_MAX_CANDIDATES == 50_000
    assert cfg.hnsw_ef_search == settings.RETRIEVAL_HNSW_EF_SEARCH == 100
    assert cfg.threshold_calibrated is False, "the 0.85 default is not calibrated until the 5d-3 sweep replaces it"


def test_read_purpose_can_never_include_drafts() -> None:
    assert retrieval.statuses_for("read", False) == ("published",)
    assert retrieval.statuses_for("manage", False) == ("published",)
    assert retrieval.statuses_for("manage", True) == ("published", "ready")
    with pytest.raises(ValueError):
        retrieval.statuses_for("read", True)


def test_a_scope_cannot_be_built_by_hand() -> None:
    with pytest.raises(TypeError):
        retrieval.RetrievalScope(tenant_id=uuid.uuid4(), pairs=frozenset(), statuses=("published",))


def test_no_api_module_uses_the_operator_scope() -> None:
    """RetrievalScope.for_operator trusts the ids it is given (CLI only). The API must build scopes from the
    authenticated user through build_retrieval_scope."""
    offenders = []
    for path in (APP_DIR / "api").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "for_operator":
                offenders.append(str(path))
            if isinstance(node, ast.Name) and node.id == "for_operator":
                offenders.append(str(path))
    assert not offenders, f"API modules must not use RetrievalScope.for_operator: {offenders}"
