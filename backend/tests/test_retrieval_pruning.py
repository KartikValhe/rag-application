"""
tests/test_retrieval_pruning.py
───────────────────────────────
Step 5d-4 on SYNTHETIC documents (the test database, the academic_world fixture, a scripted embedder):

  * frequency pruning of the keyword query: a word that is in most chunks ("thing", standing for ची, आहेत,
    what, is, the) ranks noise on the keyword side; with RETRIEVAL_KEYWORD_MAX_DF the right chunk ranks first,
    without it it does not;
  * a rare identifier (zx9qk) survives pruning and is still found by the keyword side;
  * the frequencies are counted over the caller's scope only, and in the statement that already counted the
    candidates (no extra round trip);
  * when every lexeme is pruned the keyword side is skipped entirely (no keyword statement is even run);
  * RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS and the RRF weights do what they say;
  * explicit weights 1.0 / 1.0 still reproduce the OLD hybrid ids and scores exactly;
  * step 5d-5: the DEFAULTS are vector only (keyword weight 0): no keyword statement and no frequency count is
    executed, and the hits equal the old "keyword weight 0" results exactly (same ids, same scores);
  * prove-it: with pruning switched off, the weights ignored, or the keyword side run at weight 0, the same
    checks FAIL.

The keyword machinery is switched OFF by default since 5d-5, so `config()` below starts from the settings with
the keyword weight set to 1.0: these tests are about that machinery.

The corpus (one document, 10 chunks, vector similarity to the query given in the comments):
  0  target   "A thing called inertia ..."            0.95   one "thing"
  1  noise    "thing thing thing thing thing thing"   0.90   the keyword side ranks it first
  2  noise    "thing thing thing about class work"    0.60
  3  noise    "thing thing about ordinary class work" 0.55
  4-7 filler  "The thing about <topic> ..."           0.50 .. 0.35
  8  rare     "... the code zx9qk ... a thing to check" 0.05
  9  no word  "Nothing else is written ..."           0.10   (no "thing")
so "thing" is in 9 of 10 chunks (0.9).
"""

import dataclasses
import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import (
    ChunkSpec,
    ScriptedEmbedder,
    SeededDocument,
    axis_vector,
    fake_embedder,
    seed_document,
)
from tests.security.retrieval_assertions import run_search

TARGET, NOISE, RARE = 0, 1, 8
CORPUS = [
    ("A thing called inertia resists any change of motion in the physics lesson today.", 0.95),
    ("thing thing thing thing thing thing", 0.90),
    ("thing thing thing about class work", 0.60),
    ("thing thing about ordinary class work", 0.55),
    ("The thing about fractions is ordinary class work today.", 0.50),
    ("The thing about decimals is ordinary class work today.", 0.45),
    ("The thing about angles is ordinary class work today.", 0.40),
    ("The thing about ratios is ordinary class work today.", 0.35),
    ("Lab manual: the sensor shows the code zx9qk when the probe is dry, a thing to check.", 0.05),
    ("Nothing else is written on this page of the notebook.", 0.10),
]

Check = Callable[[], Awaitable[None]]


def config(**changes: Any) -> retrieval.RetrievalConfig:
    """The settings with the keyword side ON (weight 1.0, the hybrid of 5d-4), plus `changes`."""
    changes.setdefault("rrf_keyword_weight", 1.0)
    return dataclasses.replace(retrieval.RetrievalConfig.from_settings(), **changes)


def production_defaults() -> retrieval.RetrievalConfig:
    """The settings exactly as the API and the harness read them (vector only since 5d-5)."""
    return retrieval.RetrievalConfig.from_settings()


async def seed_corpus(raw_db: AsyncSession, w: WorldTenant) -> tuple[SeededDocument, ScriptedEmbedder]:
    embedder = ScriptedEmbedder({})
    doc = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            ChunkSpec(text, vector=axis_vector(sim, axis), id=ordered_id(axis))
            for axis, (text, sim) in enumerate(CORPUS, start=1)
        ],
    )
    return doc, embedder


def ordered_id(position: int) -> uuid.UUID:
    """Ids that sort in corpus order (the top byte is the position), so every tie between equal keyword scores
    breaks the same way in every run; the low bits are random so the id is still unique."""
    return uuid.UUID(int=(position << 120) | random.getrandbits(119))


def order(doc: SeededDocument, result: retrieval.RetrievalResult) -> list[int]:
    index = {c.id: i for i, c in enumerate(doc.chunks)}
    return [index[h.chunk_id] for h in result.hits]


# ── frequency pruning ────────────────────────────────────────────────────────


async def pruning_puts_the_target_first(w: WorldTenant, doc: SeededDocument, embedder: ScriptedEmbedder) -> None:
    """The check the prove-it test runs with pruning switched off."""
    result = await run_search(w, w.t1, "thing plank", embedder, config=config(keyword_max_df=0.5))
    assert result.hits[0].chunk_id == doc.chunk_id(TARGET), f"got {order(doc, result)}"
    assert result.keyword_ranked == (), "the common word is gone and 'plank' matches nothing"


async def test_the_common_word_misleads_the_keyword_side_until_it_is_pruned(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    off = await run_search(w, w.t1, "thing plank", embedder, config=config(keyword_max_df=1.0))
    assert off.hits[0].chunk_id == doc.chunk_id(NOISE), "pruning off: the noisy chunk beats the vector's first"
    assert off.hits[0].chunk_id != doc.chunk_id(TARGET)
    assert [h.chunk_id for h in off.hits].index(doc.chunk_id(TARGET)) > 1
    assert off.vector_ranked[0] == doc.chunk_id(TARGET)
    await pruning_puts_the_target_first(w, doc, embedder)


async def test_pruning_on_leaves_a_vector_only_ranking_when_nothing_else_matches(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    result = await run_search(w, w.t1, "thing plank", embedder, config=config(keyword_max_df=0.5))
    assert [h.chunk_id for h in result.hits] == list(result.vector_ranked[: len(result.hits)])
    assert all(h.keyword_rank is None and h.keyword_matched_terms is None for h in result.hits)


async def test_a_rare_identifier_survives_pruning_and_is_found_by_the_keyword_side(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    off = await run_search(w, w.t1, "thing zx9qk", embedder, config=config(keyword_max_df=1.0))
    assert doc.chunk_id(RARE) not in {h.chunk_id for h in off.hits}, "precondition: noise hides the code today"
    on = await run_search(w, w.t1, "thing zx9qk", embedder, config=config(keyword_max_df=0.5))
    assert on.keyword_ranked == (doc.chunk_id(RARE),), "only the rare word is searched"
    top = on.hits[0]
    assert top.chunk_id == doc.chunk_id(RARE)
    assert top.keyword_rank == 1 and top.vector_rank == 10
    assert top.keyword_matched_terms == 1, "matched terms count the surviving lexemes only"
    assert on.query_lexeme_count == 2, "the question still has two words"


async def test_the_frequencies_are_counted_over_the_callers_scope_only(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """'kinetic' is in 1 of the 10 chunks of this pair (rare) but in 30 chunks of another pair of the same
    tenant. Counted over the whole tenant it would look common (31 of 40) and be pruned."""
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    mine = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[ChunkSpec("The kinetic energy of a moving body.", vector=axis_vector(0.20, 1))]
        + [ChunkSpec(f"Plain class note number {i}.", vector=axis_vector(0.10, 2 + i)) for i in range(9)],
    )
    await seed_document(
        raw_db, w, subject="Physics", classes=("10-B",), uploader=w.t2, embedder=embedder,
        chunks=[ChunkSpec(f"kinetic kinetic note {i}", vector=axis_vector(0.10, 20 + i)) for i in range(30)],
    )
    result = await run_search(w, w.t1, "kinetic", embedder, config=config(keyword_max_df=0.3))
    assert result.keyword_ranked == (mine.chunk_id(0),)
    assert result.candidate_count == 10


async def test_pruning_adds_no_database_round_trip(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    _, embedder = await seed_corpus(raw_db, w)
    off, on = await recorded(w, embedder, "thing plank", config(keyword_max_df=1.0)), await recorded(
        w, embedder, "thing plank", config(keyword_max_df=0.5)
    )
    assert len(on) == len(off), "the frequencies come with the statement that counts the candidates"
    assert not any("lexq" in s for s in off), "pruning off: the default statements are untouched"
    assert sum("lexq" in s for s in on) == 1


async def recorded(w: WorldTenant, embedder: Any, query: str, cfg: retrieval.RetrievalConfig) -> list[str]:
    """The text of every statement one search executes."""
    statements: list[str] = []
    async with tenant_session_context(str(w.id)) as db:
        real_execute = db.execute

        async def recording_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(str(statement))
            return await real_execute(statement, *args, **kwargs)

        scope = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage")
        db.execute = recording_execute  # type: ignore[method-assign]
        await retrieval.search(db, scope, query, retrieval.QueryEmbedder.from_embedder(embedder), cfg)
    return statements


async def test_the_keyword_side_is_skipped_when_every_lexeme_is_pruned(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    cfg = config(keyword_max_df=0.5)
    result = await run_search(w, w.t1, "thing", embedder, config=cfg)
    assert result.keyword_ranked == ()
    assert all(h.keyword_rank is None and h.keyword_matched_terms is None for h in result.hits)
    assert [h.chunk_id for h in result.hits] == list(result.vector_ranked[: len(result.hits)])
    assert result.keyword_coverage is None
    statements = await recorded(w, embedder, "thing", cfg)
    assert not any("ts_rank_cd" in s for s in statements), "no keyword statement ran at all"
    assert any("ts_rank_cd" in s for s in await recorded(w, embedder, "thing", config(keyword_max_df=1.0)))
    assert order(doc, result)[0] == TARGET, "the vector side alone puts the target first"


@pytest.mark.parametrize("query", ["thing plank", "thing zx9qk", "thing"])
async def test_pruning_gives_the_same_hits_on_the_hnsw_path(
    academic_world: AcademicWorld, raw_db: AsyncSession, query: str
) -> None:
    """The frequencies and the skipped keyword side do not depend on the vector path."""
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    exact = await run_search(w, w.t1, query, embedder, config=config(keyword_max_df=0.5, exact_max_candidates=50_000))
    hnsw = await run_search(w, w.t1, query, embedder, config=config(keyword_max_df=0.5, exact_max_candidates=0))
    assert (exact.path, hnsw.path) == ("exact", "hnsw")
    assert order(doc, hnsw) == order(doc, exact)
    assert hnsw.keyword_ranked == exact.keyword_ranked


async def test_a_question_that_is_all_common_words_still_answers_from_the_vector_side(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    _, embedder = await seed_corpus(raw_db, w)
    result = await run_search(w, w.t1, "thing", embedder, config=config(keyword_max_df=0.5))
    assert result.sufficient is True and result.max_similarity is not None and result.max_similarity > 0.9


# ── minimum matched terms ────────────────────────────────────────────────────


async def test_a_chunk_enters_the_keyword_ranking_only_with_enough_matched_terms(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    one = await run_search(w, w.t1, "thing zx9qk", embedder, config=config(keyword_min_matched_terms=1))
    assert len(one.keyword_ranked) == 9 and doc.chunk_id(RARE) in one.keyword_ranked
    two = await run_search(w, w.t1, "thing zx9qk", embedder, config=config(keyword_min_matched_terms=2))
    assert two.keyword_ranked == (doc.chunk_id(RARE),), "only the chunk that has both words"
    assert two.hits[0].keyword_matched_terms == 2


async def test_min_matched_terms_counts_the_pruned_lexemes_so_it_can_empty_the_keyword_side(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    cfg = config(keyword_max_df=0.5, keyword_min_matched_terms=2)
    result = await run_search(w, w.t1, "thing zx9qk", embedder, config=cfg)
    assert result.keyword_ranked == (), "one lexeme is left and two are needed"
    assert not any("ts_rank_cd" in s for s in await recorded(w, embedder, "thing zx9qk", cfg))


# ── weights ──────────────────────────────────────────────────────────────────


async def weights_decide_the_order(w: WorldTenant, doc: SeededDocument, embedder: ScriptedEmbedder) -> None:
    """The check the prove-it test runs with the weights ignored."""
    default = await run_search(w, w.t1, "thing plank", embedder, config=config())
    assert default.hits[0].chunk_id == doc.chunk_id(NOISE)
    heavy = await run_search(w, w.t1, "thing plank", embedder, config=config(rrf_vector_weight=10.0))
    assert heavy.hits[0].chunk_id == doc.chunk_id(TARGET), f"got {order(doc, heavy)}"
    vector_only = await run_search(w, w.t1, "thing plank", embedder, config=config(rrf_keyword_weight=0.0))
    assert [h.chunk_id for h in vector_only.hits] == list(vector_only.vector_ranked[: len(vector_only.hits)])
    assert [h.fused_score for h in vector_only.hits] == [1.0 / (60 + h.vector_rank) for h in vector_only.hits if h.vector_rank]


async def test_the_weights_change_the_fusion(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    await weights_decide_the_order(w, doc, embedder)


async def test_a_zero_keyword_weight_is_the_vector_only_baseline_and_runs_no_keyword_side(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Changed deliberately in 5d-5: before, the keyword side still ran at weight 0 (9 chunks ranked, no say).
    Now a side without a say is not run at all, so nothing is ranked or reported for it."""
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    result = await run_search(w, w.t1, "thing plank", embedder, config=config(rrf_keyword_weight=0.0))
    assert order(doc, result) == [0, 1, 2, 3, 4, 5]
    assert result.keyword_ranked == ()
    assert all(h.keyword_rank is None and h.keyword_matched_terms is None for h in result.hits)


# ── explicit 1.0 / 1.0 still reproduce the old hybrid ────────────────────────

# What the code returned on this corpus with the old defaults (captured before 5d-4, kept through 5d-5):
# chunk indexes in hit order with both weights 1.0.
TODAY_THING_PLANK = [1, 2, 3, 0, 4, 5]
TODAY_THING_CODE = [1, 2, 3, 0, 4, 5]


@pytest.mark.parametrize(("query", "today"), [("thing plank", TODAY_THING_PLANK), ("thing zx9qk", TODAY_THING_CODE)])
async def test_explicit_weights_one_and_one_reproduce_the_old_hybrid_ids_and_scores_exactly(
    academic_world: AcademicWorld, raw_db: AsyncSession, query: str, today: list[int]
) -> None:
    """Regression for 5d-5: the old default (hybrid) is still one setting away, bit for bit."""
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    hybrid = await run_search(
        w, w.t1, query, embedder,
        config=dataclasses.replace(production_defaults(), rrf_vector_weight=1.0, rrf_keyword_weight=1.0),
    )
    assert order(doc, hybrid) == today
    assert len(hybrid.keyword_ranked) == 9, "the keyword side ran"
    for h in hybrid.hits:  # the unweighted formula, bit for bit
        v, k = h.vector_rank, h.keyword_rank
        assert h.fused_score == (1.0 / (60 + v) if v else 0.0) + (1.0 / (60 + k) if k else 0.0)
    neutral = await run_search(
        w, w.t1, query, embedder,
        config=config(keyword_max_df=1.0, rrf_vector_weight=1.0, rrf_keyword_weight=1.0, keyword_min_matched_terms=1),
    )
    assert neutral == hybrid, "explicit neutral values are the same as the old defaults"


# ── step 5d-5: the defaults are vector only ──────────────────────────────────

# What the code returned BEFORE 5d-5 with the keyword weight set to 0 (the "vector only" row of --compare):
# chunk indexes in hit order; each fused score was 1.0 / (60 + vector rank) + 0.0.
TODAY_VECTOR_ONLY = [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("query", ["thing plank", "thing zx9qk", "thing"])
async def test_the_defaults_equal_the_old_keyword_weight_zero_results_exactly(
    academic_world: AcademicWorld, raw_db: AsyncSession, query: str
) -> None:
    """Same ids and same scores as the old code at keyword weight 0. The old code ran the keyword side and
    gave it no say; fusing the keyword ranking of a hybrid run with weight 0 is exactly that computation."""
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    default = await run_search(w, w.t1, query, embedder, config=production_defaults())
    assert order(doc, default) == TODAY_VECTOR_ONLY
    assert [h.fused_score for h in default.hits] == [1.0 / (60 + p) + 0.0 for p in range(1, 7)]

    hybrid = await run_search(w, w.t1, query, embedder, config=config())
    assert hybrid.vector_ranked == default.vector_ranked, "the vector side is the same statement either way"
    old = retrieval.select_hits(
        retrieval.rrf_fuse(hybrid.vector_ranked, hybrid.keyword_ranked, 60, 1.0, 0.0), hybrid.vector_ranked, 6
    )
    assert [(h.chunk_id, h.fused_score, h.vector_rank) for h in default.hits] == [
        (f.id, f.score, f.vector_rank) for f in old
    ]
    hybrid_sims = {h.chunk_id: h.vector_similarity for h in hybrid.hits}
    shared = [h for h in default.hits if h.chunk_id in hybrid_sims]
    assert shared and all(h.vector_similarity == hybrid_sims[h.chunk_id] for h in shared)
    assert (default.sufficient, default.reason, default.max_similarity) == (
        hybrid.sufficient, hybrid.reason, hybrid.max_similarity,
    )


async def default_runs_no_keyword_statement(w: WorldTenant, embedder: ScriptedEmbedder) -> None:
    """The check the prove-it test runs with the keyword side forced on at weight 0."""
    for cfg in (production_defaults(), dataclasses.replace(production_defaults(), keyword_max_df=0.3)):
        assert cfg.rrf_keyword_weight == 0.0
        joined = "\n".join(await recorded(w, embedder, "thing zx9qk", cfg))
        assert "ts_rank_cd" not in joined, "no keyword statement is executed at weight 0"
        assert ":tsq" not in joined and ":kcap" not in joined, "no keyword cap work"
        assert "lexq" not in joined, "no per-word frequency count either (pruning has nothing to prune for)"
        assert "content_tsv @@" not in joined


async def test_the_default_runs_no_keyword_statement_at_all(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    _, embedder = await seed_corpus(raw_db, w)
    await default_runs_no_keyword_statement(w, embedder)
    hybrid = "\n".join(await recorded(w, embedder, "thing plank", config()))
    assert hybrid.count("ts_rank_cd") == 1 and "lexq" not in hybrid and "min_terms" not in hybrid, (
        "with the keyword weight 1.0 the statements are the old hybrid ones, with none of the 5d-4 machinery"
    )


async def test_prove_it_running_the_keyword_side_at_weight_zero_fails_the_check(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    _, embedder = await seed_corpus(raw_db, w)

    async def check() -> None:
        await default_runs_no_keyword_statement(w, embedder)

    def always(cfg: retrieval.RetrievalConfig) -> bool:
        return True

    await on_then_off(check, lambda: monkeypatch.setattr(retrieval, "keyword_side_runs", always))


async def test_settings_changes_reach_the_search(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RetrievalConfig.from_settings reads the 5d-4 settings (the API and the harness use it)."""
    from app.core.config import settings

    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    monkeypatch.setattr(settings, "RETRIEVAL_RRF_KEYWORD_WEIGHT", 1.0)
    noisy = await run_search(w, w.t1, "thing plank", embedder, config=production_defaults())
    assert noisy.hits[0].chunk_id == doc.chunk_id(NOISE), "the keyword weight setting reached the search"
    monkeypatch.setattr(settings, "RETRIEVAL_KEYWORD_MAX_DF", 0.5)
    result = await run_search(w, w.t1, "thing plank", embedder, config=production_defaults())
    assert result.hits[0].chunk_id == doc.chunk_id(TARGET)


async def test_the_fake_embedder_world_still_ranks_the_right_chunk_with_pruning_on(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Pruning must not break an ordinary corpus: with the lexical FakeEmbedder the right chunk stays first."""
    w = academic_world.a
    embedder = fake_embedder()
    mine = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            "Osmosis is the movement of water across a semi permeable membrane.",
            "A prime number has exactly two divisors: one and itself.",
            "The French Revolution began in 1789 with the storming of the Bastille.",
            "Newton's second law states that force equals mass times acceleration.",
        ],
    )
    result = await run_search(w, w.t1, "What is osmosis across a membrane?", embedder, config=config(keyword_max_df=0.5))
    assert result.hits[0].chunk_id == mine.chunk_id(0)


# ── prove-it: switch the feature off and the same checks FAIL ────────────────


async def on_then_off(check: Check, switch_off: Callable[[], None]) -> None:
    await check()  # protection on: passes
    switch_off()
    with pytest.raises(AssertionError):
        await check()


async def test_prove_it_pruning_switched_off_fails_the_crafted_check(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)

    async def check() -> None:
        await pruning_puts_the_target_first(w, doc, embedder)

    def keep_everything(lexemes: Any, doc_freq: Any, candidate_count: Any, max_df: Any) -> list[str]:
        return list(lexemes)

    await on_then_off(check, lambda: monkeypatch.setattr(retrieval, "prune_lexemes", keep_everything))


async def test_prove_it_weights_ignored_fails_the_weight_check(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)
    real = retrieval.rrf_fuse

    async def check() -> None:
        await weights_decide_the_order(w, doc, embedder)

    def ignore_weights(vector_ids: Any, keyword_ids: Any, rrf_k: Any, vector_weight: float = 1.0, keyword_weight: float = 1.0) -> Any:
        return real(vector_ids, keyword_ids, rrf_k)

    await on_then_off(check, lambda: monkeypatch.setattr(retrieval, "rrf_fuse", ignore_weights))


async def test_prove_it_min_matched_terms_ignored_fails_the_check(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    doc, embedder = await seed_corpus(raw_db, w)

    async def check() -> None:
        two = await run_search(w, w.t1, "thing zx9qk", embedder, config=config(keyword_min_matched_terms=2))
        assert two.keyword_ranked == (doc.chunk_id(RARE),)

    real = retrieval.search_sql

    def drop_min_terms(schema: str, *, hnsw: bool, dim: int, **kw: Any) -> str:
        return real(schema, hnsw=hnsw, dim=dim)

    await on_then_off(check, lambda: monkeypatch.setattr(retrieval, "search_sql", drop_min_terms))

