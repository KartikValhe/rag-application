"""
tests/test_retrieval_calibration_db.py
──────────────────────────────────────
Step 5d-5 on SYNTHETIC documents (the test database, the academic_world fixture, a scripted embedder):
threshold_calibrated is DERIVED from the active model key in every place a search result leaves the service.

  * search() (the test search) and search_for_chat() (the chat entry point) report true when the embedder's
    model key equals RETRIEVAL_CALIBRATED_MODEL_KEY, false when it does not, also for an insufficient result
    and for an empty scope;
  * the default threshold (0.84) decides sufficiency;
  * prove-it: with the derivation replaced by a constant true, the false-case check FAILS.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.tenant_db import tenant_session_context
from app.ml.model_keys import E5_SMALL_MODEL_KEY
from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import ChunkSpec, ScriptedEmbedder, axis_vector, seed_document
from tests.security.retrieval_assertions import run_search

OTHER_KEY = "BAAI/bge-m3@0000000000000000000000000000000000000000"


async def seed(raw_db: AsyncSession, w: WorldTenant, sim: float) -> ScriptedEmbedder:
    embedder = ScriptedEmbedder({})  # carries the e5-small key, like the real model
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
                        chunks=[ChunkSpec("Osmosis moves water across a membrane.", vector=axis_vector(sim, 1))])
    return embedder


async def chat_search(w: WorldTenant, embedder: ScriptedEmbedder) -> retrieval.RetrievalResult:
    async with tenant_session_context(str(w.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, w.s1, purpose="read")
        return await retrieval.search_for_chat(
            db, scope, "osmosis", retrieval.QueryEmbedder.from_embedder(embedder), retrieval.RetrievalConfig.from_settings()
        )


async def flags(w: WorldTenant, embedder: ScriptedEmbedder) -> tuple[bool, bool]:
    default = retrieval.RetrievalConfig.from_settings()
    test_search = await run_search(w, w.t1, "osmosis", embedder, config=default)
    chat = await chat_search(w, embedder)
    return test_search.threshold_calibrated, chat.threshold_calibrated


async def test_both_entry_points_report_calibrated_for_the_calibrated_model(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    embedder = await seed(raw_db, w, 0.95)
    assert embedder.model_id == E5_SMALL_MODEL_KEY == settings.RETRIEVAL_CALIBRATED_MODEL_KEY
    assert await flags(w, embedder) == (True, True)
    chat = await chat_search(w, embedder)
    assert chat.sufficient and chat.threshold == 0.84 and chat.hits


async def false_case(w: WorldTenant, embedder: ScriptedEmbedder, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check the prove-it test runs with the derivation replaced by a constant."""
    monkeypatch.setattr(settings, "RETRIEVAL_CALIBRATED_MODEL_KEY", OTHER_KEY)
    assert await flags(w, embedder) == (False, False), "the active model is not the one the threshold was measured for"
    monkeypatch.setattr(settings, "RETRIEVAL_CALIBRATED_MODEL_KEY", "")
    assert await flags(w, embedder) == (False, False), "no calibrated model at all"


async def test_both_entry_points_report_not_calibrated_for_another_model(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    embedder = await seed(raw_db, w, 0.95)
    await false_case(w, embedder, monkeypatch)


async def test_an_insufficient_result_and_an_empty_scope_carry_the_derived_flag_too(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    embedder = await seed(raw_db, w, 0.83)  # below the 0.84 default
    below = await run_search(w, w.t1, "osmosis", embedder, config=retrieval.RetrievalConfig.from_settings())
    assert (below.sufficient, below.reason, below.threshold, below.threshold_calibrated) == (
        False, "below_threshold", 0.84, True,
    )
    chat = await chat_search(w, embedder)
    assert (chat.sufficient, chat.hits, chat.threshold_calibrated) == (False, (), True)
    nothing = await run_search(academic_world.b, academic_world.b.s1, "osmosis", embedder, purpose="read")
    assert nothing.candidate_count == 0 and nothing.threshold_calibrated is True
    monkeypatch.setattr(settings, "RETRIEVAL_CALIBRATED_MODEL_KEY", OTHER_KEY)
    again = await run_search(w, w.t1, "osmosis", embedder, config=retrieval.RetrievalConfig.from_settings())
    assert again.threshold_calibrated is False


async def test_prove_it_a_constant_true_fails_the_false_case(
    academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a
    embedder = await seed(raw_db, w, 0.95)
    await false_case(w, embedder, monkeypatch)  # derived: passes

    def always(model_key: str, config: retrieval.RetrievalConfig) -> bool:
        return True

    monkeypatch.setattr(retrieval, "is_threshold_calibrated", always)
    with pytest.raises(AssertionError):
        await false_case(w, embedder, monkeypatch)
