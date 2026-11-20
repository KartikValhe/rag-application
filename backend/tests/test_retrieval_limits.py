"""
tests/test_retrieval_limits.py
──────────────────────────────
The limits of one search (step 5d-1b, review findings 3, 5 and 6):

  * statement_timeout: set (transaction-local) before the count statement, restored after a search; a
    statement over the limit is the clean domain error search_timeout (no SQL, no query text). Prove-it:
    without the setting the same slow statement completes;
  * the keyword side ranks at most RETRIEVAL_KEYWORD_MAX_CANDIDATES matching chunks (the lowest ids), so a
    query of common words cannot rank every chunk of a tenant. Prove-it: without the cap the ranking differs;
  * NaN distances (a stored zero vector) never reach the vector side, on both paths;
  * the HNSW path: hnsw.iterative_scan is read back after it is set (a wrong value fails clearly), the
    settings are transaction-local, and with sequential scans off the planner uses the partial HNSW index.
"""

import dataclasses
import math
import random
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.ml.model_keys import E5_SMALL_DIM, E5_SMALL_MODEL_KEY
from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import (
    ChunkSpec,
    ScriptedEmbedder,
    axis_vector,
    fake_embedder,
    seed_document,
)
from tests.security.retrieval_assertions import run_search, search_config

SLOW_COUNT_TAIL = "\nSELECT (SELECT count(*) FROM cand) FROM pg_sleep(0.4)"
HNSW_INDEX = "ix_chunk_embeddings_hnsw_e5_small"


async def _seed(raw_db: AsyncSession, w: WorldTenant, n: int = 3) -> None:
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=fake_embedder(),
                        chunks=[f"Fractions lesson {i} zq{uuid.uuid4().hex[:8]}." for i in range(n)])


# ── statement_timeout ────────────────────────────────────────────────────────


async def _slow_search(w: WorldTenant, timeout_ms: int) -> retrieval.RetrievalResult:
    cfg = dataclasses.replace(search_config(), statement_timeout_ms=timeout_ms)
    return await run_search(w, w.t1, "fractions secretquerytext", fake_embedder(), config=cfg)


async def test_a_statement_over_the_timeout_is_a_clean_search_timeout(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    await _seed(raw_db, w)
    monkeypatch.setattr(retrieval, "_COUNT_TAIL", SLOW_COUNT_TAIL)
    with pytest.raises(retrieval.SearchUnavailable) as exc:
        await _slow_search(w, 50)
    err = exc.value
    assert err.code == "search_timeout" and err.http_status == 503
    assert str(err) == "search_timeout" and repr(err) == "SearchUnavailable('search_timeout')"
    assert err.__cause__ is None and err.__suppress_context__, "the SQL error (statement and parameters) is not chained"


async def test_prove_it_without_the_timeout_the_slow_statement_completes(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same slow count with the timeout statement switched off runs to the end: the protection above is
    the setting, not something else."""
    w = academic_world.a
    await _seed(raw_db, w)
    monkeypatch.setattr(retrieval, "_COUNT_TAIL", SLOW_COUNT_TAIL)
    monkeypatch.setattr(retrieval, "SET_STATEMENT_TIMEOUT_SQL", "SELECT CAST(:ms AS text)")
    result = await _slow_search(w, 50)
    assert result.candidate_count == 3


async def test_the_timeout_is_set_before_the_count_and_restored_after_the_search(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    await _seed(raw_db, w)
    seen: list[str] = []
    async with tenant_session_context(str(w.id)) as db:
        await db.execute(text("SELECT set_config('statement_timeout', '7min', true)"))
        scope = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage")
        real_execute = db.execute

        async def spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
            if "SELECT count(*) FROM cand" in str(statement):
                seen.append((await real_execute(text("SELECT current_setting('statement_timeout')"))).scalar_one())
            return await real_execute(statement, *args, **kwargs)

        db.execute = spy  # type: ignore[method-assign]
        cfg = dataclasses.replace(search_config(), statement_timeout_ms=1234)
        await retrieval.search(db, scope, "fractions", retrieval.QueryEmbedder.from_embedder(fake_embedder()), cfg)
        db.execute = real_execute  # type: ignore[method-assign]
        after = (await db.execute(text("SELECT current_setting('statement_timeout')"))).scalar_one()
    assert seen == ["1234ms"]
    assert after == "7min", "the caller's own statement_timeout is restored"


# ── keyword candidate cap ────────────────────────────────────────────────────


async def test_the_keyword_side_ranks_at_most_the_capped_candidates(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Three weak matches have the LOWEST ids, three strong matches the highest. With a cap of 3 only the
    three lowest ids are ranked; without the cap (prove-it) the strong ones win."""
    w = academic_world.a
    low = [uuid.UUID(int=random.getrandbits(120)) for _ in range(3)]
    high = [uuid.UUID(int=(0xFF << 120) | random.getrandbits(120)) for _ in range(3)]
    embedder = ScriptedEmbedder({})
    await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            *(ChunkSpec(f"capword with a long tail of other words number {i} here", id=i_d) for i, i_d in enumerate(low)),
            *(ChunkSpec(f"capword capword capword {i}", id=i_d) for i, i_d in enumerate(high)),
        ],
    )
    base = search_config()
    capped = await run_search(w, w.t1, "capword", embedder, config=dataclasses.replace(base, keyword_k=3, keyword_max_candidates=3))
    assert set(capped.keyword_ranked) == set(low)
    uncapped = await run_search(w, w.t1, "capword", embedder, config=dataclasses.replace(base, keyword_k=3))
    assert set(uncapped.keyword_ranked) == set(high), "prove-it: without the cap the ranking is different"


# ── NaN distances ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hnsw", [False, True])
async def test_a_zero_stored_vector_never_reaches_the_vector_side(
    hnsw: bool, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    doc = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[ChunkSpec("Zero vector chunk nanword.", vector=[0.0] * E5_SMALL_DIM),
                ChunkSpec("A normal chunk.", vector=axis_vector(0.5, 1))],
    )
    cfg = search_config()
    if hnsw:
        cfg = dataclasses.replace(cfg, exact_max_candidates=0)
    result = await run_search(w, w.t1, "nanword", embedder, config=cfg)
    assert result.path == ("hnsw" if hnsw else "exact")
    assert result.vector_ranked == (doc.chunk_id(1),), "the NaN distance of the zero vector is excluded"
    assert result.max_similarity is not None and math.isfinite(result.max_similarity)
    zero = next(h for h in result.hits if h.chunk_id == doc.chunk_id(0))
    assert zero.vector_similarity is None and zero.keyword_rank == 1, "still found by the keyword side"


# ── HNSW path ────────────────────────────────────────────────────────────────


def _hnsw_config() -> retrieval.RetrievalConfig:
    return dataclasses.replace(search_config(), exact_max_candidates=0)


async def test_the_hnsw_settings_are_read_back_and_stay_transaction_local(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    await _seed(raw_db, w)
    async with tenant_session_context(str(w.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage")
        result = await retrieval.search(db, scope, "fractions", retrieval.QueryEmbedder.from_embedder(fake_embedder()), _hnsw_config())
        inside = (await db.execute(text("SELECT current_setting('hnsw.iterative_scan'), current_setting('hnsw.ef_search')"))).one()
    async with tenant_session_context(str(w.id)) as db:
        outside = (await db.execute(text("SELECT current_setting('hnsw.iterative_scan', true)"))).scalar_one()
    assert result.path == "hnsw"
    assert tuple(inside) == ("relaxed_order", "100")
    assert outside in (None, "", "off"), "the next transaction on the pooled connection starts from the default"


async def test_a_wrong_iterative_scan_value_fails_clearly(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What an older pgvector (no hnsw.iterative_scan) would give: the read-back is not relaxed_order."""
    w = academic_world.a
    await _seed(raw_db, w)
    monkeypatch.setattr(retrieval, "HNSW_ITERATIVE_SCAN", "strict_order")
    with pytest.raises(RuntimeError, match="relaxed_order"):
        await run_search(w, w.t1, "fractions", fake_embedder(), config=_hnsw_config())


async def test_an_old_pgvector_is_refused_on_the_hnsw_path(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    await _seed(raw_db, w)
    monkeypatch.setattr(retrieval, "MIN_PGVECTOR_FOR_HNSW", (99, 0, 0))
    with pytest.raises(RuntimeError, match="pgvector"):
        await run_search(w, w.t1, "fractions", fake_embedder(), config=_hnsw_config())
    exact = await run_search(w, w.t1, "fractions", fake_embedder())
    assert exact.path == "exact", "the exact path does not need the newer pgvector"


async def test_with_cheaper_plans_off_the_planner_uses_the_partial_hnsw_index(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """The search statement as it is sent CAN use ix_chunk_embeddings_hnsw_e5_small (its expression and its
    partial predicate match). Measured on tiny tables (recorded, not asserted): with only enable_seqscan off
    the planner still prefers the primary key plus a sort, because a sort of a handful of rows is cheaper.
    With enable_sort off as well, the ordered HNSW index scan is the only plan left and must be used.
    Whether the planner chooses it at real scale stays backlog B95."""
    w = academic_world.a
    await _seed(raw_db, w, n=5)
    plans: dict[str, str] = {}
    for label, switches in (("seqscan_off", ("enable_seqscan",)), ("seqscan_and_sort_off", ("enable_seqscan", "enable_sort"))):
        async with tenant_session_context(str(w.id)) as db:
            scope = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage")
            schema, _ = (await db.execute(text(retrieval.VECTOR_EXTENSION_SQL))).one()
            for switch in switches:
                await db.execute(text("SELECT set_config(:name, 'off', true)"), {"name": switch})
            applied = (await db.execute(
                text(retrieval.HNSW_SETTINGS_SQL), {"mode": retrieval.HNSW_ITERATIVE_SCAN, "ef": "100"}
            )).one()[2]
            assert applied == "relaxed_order"
            pairs = scope.ordered_pairs
            params = {
                "t": w.id, "class_ids": [p[0] for p in pairs], "subject_ids": [p[1] for p in pairs],
                "statuses": ["published"], "qv": "[" + ",".join(["0.05"] * E5_SMALL_DIM) + "]",
                "model": E5_SMALL_MODEL_KEY, "dim": E5_SMALL_DIM, "vk": 20, "tsq": "'fractions'",
                "lexemes": ["fractions"], "kk": 20, "kcap": 2000,
            }
            sql = "EXPLAIN " + retrieval.search_sql(schema, hnsw=True, dim=E5_SMALL_DIM)
            plans[label] = "\n".join(r[0] for r in (await db.execute(text(sql), params)).all())
    print(f"HNSW index chosen with only enable_seqscan off: {HNSW_INDEX in plans['seqscan_off']}")
    assert HNSW_INDEX in plans["seqscan_and_sort_off"], plans["seqscan_and_sort_off"]
