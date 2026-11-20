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
from app.ml.model_keys import E5_SMALL_MODEL_KEY
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
    # the clause needs the keyword side switched on (off by default since 5d-5)
    on = dataclasses.replace(CONFIG, min_keyword_coverage=0.6, rrf_keyword_weight=1.0)
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
    assert cfg.min_similarity == settings.RETRIEVAL_MIN_SIMILARITY == 0.84  # calibrated in 5d-5
    assert cfg.min_keyword_coverage == settings.RETRIEVAL_MIN_KEYWORD_COVERAGE == 0.0
    assert cfg.exact_max_candidates == settings.RETRIEVAL_EXACT_MAX_CANDIDATES == 50_000
    assert cfg.hnsw_ef_search == settings.RETRIEVAL_HNSW_EF_SEARCH == 100
    assert cfg.statement_timeout_ms == settings.RETRIEVAL_STATEMENT_TIMEOUT_MS == 5000
    assert cfg.keyword_max_candidates == settings.RETRIEVAL_KEYWORD_MAX_CANDIDATES == 2000
    # 5d-5: calibrated for one model; whether a result is calibrated is derived from its model key
    assert cfg.calibrated_model_key == settings.RETRIEVAL_CALIBRATED_MODEL_KEY == E5_SMALL_MODEL_KEY


def test_read_purpose_can_never_include_drafts() -> None:
    assert retrieval.statuses_for("read", False) == ("published",)
    assert retrieval.statuses_for("manage", False) == ("published",)
    assert retrieval.statuses_for("manage", True) == ("published", "ready")
    with pytest.raises(ValueError):
        retrieval.statuses_for("read", True)


def _issued_scope(purpose: retrieval.Purpose = "manage") -> tuple[retrieval.RetrievalScope, list[tuple[uuid.UUID, uuid.UUID]]]:
    """A scope issued the way build_retrieval_scope issues one (the DB lookups are what the DB tests cover)."""
    c1, c2, s1, s2 = _ids(4)
    pairs = [(c1, s1), (c1, s2), (c2, s1)]
    return retrieval._issue_scope(uuid.uuid4(), frozenset(pairs), ("published",), purpose), pairs


def test_a_scope_cannot_be_built_by_hand() -> None:
    with pytest.raises(TypeError):
        retrieval.RetrievalScope(tenant_id=uuid.uuid4(), pairs=frozenset(), statuses=("published",), purpose="read")


def test_a_scope_cannot_be_widened_through_replace_or_a_copy() -> None:
    """dataclasses.replace, copy, deepcopy and pickle all bypass the builders, so each of them must fail."""
    import copy
    import pickle

    scope, _ = _issued_scope()
    foreign = (uuid.uuid4(), uuid.uuid4())
    with pytest.raises(TypeError):
        dataclasses.replace(scope, pairs=scope.pairs | {foreign})
    with pytest.raises(TypeError):
        dataclasses.replace(scope, tenant_id=uuid.uuid4())
    with pytest.raises(TypeError):
        copy.copy(scope)
    with pytest.raises(TypeError):
        copy.deepcopy(scope)
    with pytest.raises(TypeError):
        pickle.dumps(scope)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.pairs = frozenset({foreign})  # type: ignore[misc]


async def test_search_refuses_a_scope_that_was_not_issued() -> None:
    """Even a scope made by bypassing __init__ (object.__new__) is refused before any database call."""
    scope, _ = _issued_scope()
    forged = object.__new__(retrieval.RetrievalScope)
    for name in ("tenant_id", "pairs", "statuses", "purpose"):
        object.__setattr__(forged, name, getattr(scope, name))
    with pytest.raises(TypeError, match="issued"):
        await retrieval.search(None, forged, "q", None, RetrievalConfig())  # type: ignore[arg-type]


def test_a_scope_records_its_purpose() -> None:
    assert _issued_scope("read")[0].purpose == "read"
    assert _issued_scope("manage")[0].purpose == "manage"
    with pytest.raises(ValueError):
        retrieval._issue_scope(uuid.uuid4(), frozenset(), ("published",), "write")  # type: ignore[arg-type]


def test_narrow_can_only_shrink_the_pairs() -> None:
    from fastapi import HTTPException

    scope, pairs = _issued_scope()
    (c1, s1), (_, s2), (c2, _) = pairs
    assert scope.narrow(class_id=c1).pairs == frozenset({(c1, s1), (c1, s2)})
    assert scope.narrow(subject_id=s1).pairs == frozenset({(c1, s1), (c2, s1)})
    assert scope.narrow(class_id=c1, subject_id=s2).pairs == frozenset({(c1, s2)})
    assert scope.narrow().pairs == scope.pairs
    narrowed = scope.narrow(class_id=c2)
    assert (narrowed.tenant_id, narrowed.statuses, narrowed.purpose) == (scope.tenant_id, scope.statuses, scope.purpose)
    assert narrowed.narrow(class_id=c2).pairs <= narrowed.pairs
    # Outside the pairs, also the cross-product pair (c2, s2) and an id of nothing: the generic 404, never a widening
    for kw in ({"class_id": uuid.uuid4()}, {"subject_id": uuid.uuid4()}, {"class_id": c2, "subject_id": s2}):
        with pytest.raises(HTTPException) as exc:
            scope.narrow(**kw)
        assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        narrowed.narrow(class_id=c1)  # a narrowed scope cannot grow back


def test_no_app_module_outside_the_scripts_uses_the_operator_scope_or_the_scope_issuer() -> None:
    """RetrievalScope.for_operator trusts the ids it is given (operator scripts only), and _issue_scope /
    _ISSUING are the private issuer. Every module under app/ except app/scripts (and the defining module)
    must build scopes from the authenticated user through build_retrieval_scope."""
    forbidden = {"for_operator", "_issue_scope", "_ISSUING", "_ISSUED"}
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel.startswith("scripts/") or rel == "services/retrieval.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = (
                node.attr if isinstance(node, ast.Attribute)
                else node.id if isinstance(node, ast.Name)
                else node.value if isinstance(node, ast.Constant) and isinstance(node.value, str)
                else node.name if isinstance(node, ast.alias)
                else None
            )
            if name in forbidden:
                offenders.append(f"{rel}: {name}")
    assert not offenders, f"only operator scripts may use the operator scope: {offenders}"


# ── chat entry point ─────────────────────────────────────────────────────────


async def test_search_for_chat_refuses_a_manage_scope() -> None:
    scope, _ = _issued_scope("manage")
    with pytest.raises(ValueError, match="read"):
        await retrieval.search_for_chat(None, scope, "q", None, RetrievalConfig())  # type: ignore[arg-type]


async def test_search_for_chat_returns_no_hits_when_insufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    scope, _ = _issued_scope("read")
    hit = retrieval.Hit(
        rank=1, chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), document_title="t", version_id=uuid.uuid4(),
        version_no=1, version_status="published", original_filename="f.pdf", page_start=1, page_end=1,
        heading_path=None, content="c", vector_rank=1, vector_similarity=0.5, keyword_rank=None,
        keyword_matched_terms=None, fused_score=0.1, injection_flags=(),
    )
    base = retrieval._result(RetrievalConfig(), "m", has_scope=True, candidate_count=1, path="exact",
                             max_similarity=0.5, hits=(hit,), vector_ranked=(hit.chunk_id,))

    async def fake_search(*args: object, **kwargs: object) -> retrieval.RetrievalResult:
        return base

    monkeypatch.setattr(retrieval, "search", fake_search)
    result = await retrieval.search_for_chat(None, scope, "q", None, RetrievalConfig())  # type: ignore[arg-type]
    assert result.sufficient is False and result.reason == "below_threshold"
    assert (result.hits, result.vector_ranked, result.keyword_ranked) == ((), (), ())
    base = dataclasses.replace(base, sufficient=True, reason="similarity", message=None)
    assert (await retrieval.search_for_chat(None, scope, "q", None, RetrievalConfig())).hits == (hit,)  # type: ignore[arg-type]


# ── hit selection and sufficiency from the returned hits ─────────────────────


def test_the_top_vector_chunk_is_always_kept_in_the_hits() -> None:
    """A is first on the vector side only; B and C are on both sides. With final_k 2 fusion keeps B and C, so
    A replaces the lowest fused hit (C)."""
    a, b, c = _ids(3)
    vector, keyword = [a, b, c], [b, c]
    fused = retrieval.rrf_fuse(vector, keyword, 60)
    assert [f.id for f in fused[:2]] == [b, c], "precondition: fusion alone drops the top vector chunk"
    assert [f.id for f in retrieval.select_hits(fused, vector, 2)] == [b, a]
    assert [f.id for f in retrieval.select_hits(fused, vector, 3)] == [b, c, a]
    assert [f.id for f in retrieval.select_hits(fused, vector, 1)] == [a]
    assert [f.id for f in retrieval.select_hits(fused, [], 2)] == [b, c]


def test_hit_stats_use_the_returned_hits_only_and_drop_non_finite_similarities() -> None:
    def hit(sim: float | None, matched: int | None) -> retrieval.Hit:
        return retrieval.Hit(
            rank=1, chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), document_title="t", version_id=uuid.uuid4(),
            version_no=1, version_status="published", original_filename="f", page_start=None, page_end=None,
            heading_path=None, content="c", vector_rank=1 if sim is not None else None, vector_similarity=sim,
            keyword_rank=1 if matched is not None else None, keyword_matched_terms=matched, fused_score=0.0,
            injection_flags=(),
        )

    assert retrieval.hit_stats([hit(0.7, None), hit(float("nan"), 2), hit(float("inf"), None)], 4) == (0.7, 0.5)
    assert retrieval.hit_stats([hit(float("nan"), None)], 4) == (None, None)
    assert retrieval.hit_stats([], 0) == (None, None)


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("k", [0, -1, 51, 10**9, True, 2.0, "6"])
def test_k_must_be_an_int_from_1_to_50(k: object) -> None:
    with pytest.raises(ValueError):
        retrieval.final_k_for(k, RetrievalConfig())  # type: ignore[arg-type]


def test_k_defaults_to_the_configured_final_k() -> None:
    assert retrieval.final_k_for(None, RetrievalConfig(final_k=6)) == 6
    assert retrieval.final_k_for(1, RetrievalConfig()) == 1
    assert retrieval.final_k_for(50, RetrievalConfig()) == 50


@pytest.mark.parametrize(
    "vector",
    [[0.0, 0.0, 0.0], [float("nan"), 1.0, 0.0], [float("inf"), 0.0, 0.0], [1.0, 0.0], [1e-300, 0.0, 0.0]],
)
def test_an_unusable_query_vector_is_refused(vector: list[float]) -> None:
    with pytest.raises(retrieval.SearchUnavailable) as exc:
        retrieval._vector_literal(vector, 3)
    assert exc.value.code == "search_unavailable"
    assert str(exc.value) == "search_unavailable"


def test_config_validates_its_combinations() -> None:
    with pytest.raises(ValueError, match="ef_search"):
        RetrievalConfig(vector_k=50, hnsw_ef_search=40)
    with pytest.raises(ValueError, match="keyword"):
        RetrievalConfig(keyword_k=20, keyword_max_candidates=10)
    with pytest.raises(ValueError, match="final_k"):
        RetrievalConfig(final_k=0)
    with pytest.raises(ValueError, match="timeout"):
        RetrievalConfig(statement_timeout_ms=0)


RETRIEVAL_ENV = (
    "RETRIEVAL_VECTOR_K", "RETRIEVAL_KEYWORD_K", "RETRIEVAL_HNSW_EF_SEARCH", "RETRIEVAL_KEYWORD_MAX_CANDIDATES",
    "RETRIEVAL_STATEMENT_TIMEOUT_MS",
)


def _fresh_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> object:
    from app.core.config import Settings

    for name in RETRIEVAL_ENV:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_settings_refuse_an_ef_search_below_the_vector_k(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="RETRIEVAL_HNSW_EF_SEARCH"):
        _fresh_settings(monkeypatch, RETRIEVAL_VECTOR_K=50, RETRIEVAL_HNSW_EF_SEARCH=40)
    with pytest.raises(ValidationError, match="RETRIEVAL_KEYWORD_MAX_CANDIDATES"):
        _fresh_settings(monkeypatch, RETRIEVAL_KEYWORD_K=30, RETRIEVAL_KEYWORD_MAX_CANDIDATES=20)
    ok = _fresh_settings(monkeypatch, RETRIEVAL_VECTOR_K=50, RETRIEVAL_HNSW_EF_SEARCH=50)
    assert ok.RETRIEVAL_HNSW_EF_SEARCH == 50  # type: ignore[attr-defined]
