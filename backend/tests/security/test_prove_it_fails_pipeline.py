"""
Prove-it-fails for the chunk / embed / index protections of step 5c-2 (design C2).

Each test first runs the scenario with the protection ON (the shared helper passes), then switches the
protection off, runs the SAME kind of scenario again and requires the helper to RAISE. That shows the helper
really checks the protection and does not pass by accident.

  1. the ownership check in the index write        -> the stale-worker helper raises.
  2. the single-published invariant (one transaction) -> a publish that commits in two steps is seen.
  3. the pages quota re-check in the index         -> the quota helper raises.
  4. the tenant scoping of embedding reuse         -> the canary helper raises.
  5. the token-bound check on every finished chunk -> the model-limit helper raises.
  6. the statement-count bound (bulk inserts)      -> the constant-statements helper raises.

TEST database only: the autouse fixture asserts current_database() ends in _test before anything runs.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slots import SlotManager
from app.core.tenant_db import tenant_session_context
from app.db.models.audit import AuditLog
from app.db.models.document import Chunk, ChunkEmbedding
from app.services import chunker, document_lifecycle
from app.services.chunker import chunk_document
from app.workers import ingest, pipeline, stages
from app.workers.db import WorkerDatabases
from tests.conftest import _TestSession
from tests.fixtures.academic_world import AcademicWorld, WorldTenant, fake_vector
from tests.fixtures.chunk_docs import flow_doc
from tests.fixtures.documents import make_document
from tests.fixtures.pipeline import (
    RecordingEmbedder,
    StatementCounter,
    count_rows,
    phased,
    prepare_for_index,
    read_vectors,
    ready_version,
    slides_doc,
    stub_stages,
)
from tests.fixtures.worker import make_context, make_queue, queued_version, version_row
from tests.security.lifecycle_assertions import (
    assert_exactly_one_published,
    observe_published_counts_during_publish,
)
from tests.security.pipeline_assertions import (
    assert_chunks_within_model_limit,
    assert_index_write_requires_ownership,
    assert_quota_breach_writes_nothing,
    assert_reuse_is_tenant_scoped,
    assert_statement_count_is_constant,
)


@pytest_asyncio.fixture(autouse=True)
async def only_the_test_database(raw_db: AsyncSession) -> AsyncIterator[None]:
    name = (await raw_db.execute(text("SELECT current_database()"))).scalar_one()
    assert name.endswith("_test"), f"prove-it-fails checks run only in the test database, not in {name!r}"
    yield


# ── 1. The ownership check in the index write ────────────────────────────────


async def test_without_the_ownership_check_a_stale_worker_writes_and_the_helper_sees_it(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(w: WorldTenant) -> tuple[Any, Any]:
        sctx, st, _ = await prepare_for_index(worker_dbs, w, raw_db, tmp_path, ingest_slots)
        await raw_db.execute(text("UPDATE app.ingest_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :j"), {"j": sctx.job.id})
        await raw_db.commit()
        assert await make_queue(worker_dbs).claim("second-worker") is not None  # another worker took the job over
        return (lambda: st.index(sctx)), (lambda: count_rows(raw_db, "chunks", sctx.version.id))

    attempt, count = await scenario(academic_world.a)
    await assert_index_write_requires_ownership(attempt, count)  # protection ON: passes

    async def no_check(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(stages, "lock_job_ownership", no_check)  # protection OFF
    attempt, count = await scenario(academic_world.b)  # (tenant A's job is still held by the 'second worker')
    with pytest.raises(AssertionError, match="no longer owned the job"):
        await assert_index_write_requires_ownership(attempt, count)


# ── 2. The single-published invariant ────────────────────────────────────────


async def test_a_publish_that_commits_in_two_steps_is_seen_by_the_single_published_helper(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    from tests.test_document_lifecycle import base_document

    w = academic_world.a

    async def scenario() -> tuple[Any, Any]:
        base = await base_document(raw_db, w, tmp_path)
        await ready_version(raw_db, base, w, tmp_path, status="published", version_no=1)
        v2 = await ready_version(raw_db, base, w, tmp_path, status="ready", version_no=2)
        return base.document.id, v2.version.id

    doc_id, v2_id = await scenario()
    assert_exactly_one_published(await observe_published_counts_during_publish(w, doc_id, v2_id))  # ON: passes

    async def two_step_publish(w_: WorldTenant, document_id: Any, version_id: Any) -> None:
        async with tenant_session_context(str(w_.id)) as db:  # the archive is committed on its own ...
            await document_lifecycle.archive_published(db, tenant_id=w_.id, document_id=document_id, retention_days=30)
        async with tenant_session_context(str(w_.id)) as db:  # ... and the new version is published afterwards
            await document_lifecycle.mark_published(db, tenant_id=w_.id, version_id=version_id, actor_id=w_.admin.id, acknowledge_flags=False)

    doc_id, v2_id = await scenario()
    counts = await observe_published_counts_during_publish(w, doc_id, v2_id, publisher=two_step_publish)
    assert counts == [1, 0, 1]  # for a moment the document had no published version
    with pytest.raises(AssertionError, match="published versions"):
        assert_exactly_one_published(counts)


# ── 3. The pages quota re-check ──────────────────────────────────────────────


async def _over_quota_scenario(
    worker_dbs: WorkerDatabases, w: WorldTenant, raw_db: AsyncSession, tmp_path: Path, slots: SlotManager
) -> tuple[Any, str, int]:
    other = await queued_version(raw_db, w, tmp_path, subject="Physics")
    await raw_db.execute(text("UPDATE app.document_versions SET status = 'published', published_at = now(), page_count = 8 WHERE id = :v"), {"v": other.version.id})
    await raw_db.execute(text("UPDATE app.ingest_jobs SET status = 'done' WHERE id = :j"), {"j": other.job.id})
    await raw_db.execute(text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"), {"v": json.dumps({"max_pages": 10}), "id": w.id})
    await raw_db.commit()
    made = await queued_version(raw_db, w, tmp_path)
    outcome = await ingest.run_one(make_context(worker_dbs, tmp_path, slots), stub_stages(slides_doc([f"slide {i}" for i in range(3)]), RecordingEmbedder()))
    v = await version_row(raw_db, made.version.id)
    return outcome, v.status, await count_rows(raw_db, "chunks", made.version.id)


async def test_without_the_pages_quota_recheck_an_over_quota_version_becomes_ready(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, status, chunks = await _over_quota_scenario(worker_dbs, academic_world.a, raw_db, tmp_path, ingest_slots)
    assert_quota_breach_writes_nothing(outcome, ingest.JobOutcome.FAILED, status, chunks)  # ON: passes

    monkeypatch.setattr(stages, "check_pages_quota", lambda *_a, **_k: None)  # protection OFF
    outcome, status, chunks = await _over_quota_scenario(worker_dbs, academic_world.b, raw_db, tmp_path, ingest_slots)
    with pytest.raises(AssertionError, match="quota re-check did not run"):
        assert_quota_breach_writes_nothing(outcome, ingest.JobOutcome.FAILED, status, chunks)


# ── 4. The tenant scoping of embedding reuse ─────────────────────────────────


async def _reuse_scenario(
    worker_dbs: WorkerDatabases, a: WorldTenant, b: WorldTenant, raw_db: AsyncSession, tmp_path: Path, slots: SlotManager, tag: str
) -> tuple[list[float], list[list[float]], int]:
    doc = slides_doc([f"Shared line {tag} one.", f"Shared line {tag} two."])
    emb = RecordingEmbedder()
    canary = fake_vector(f"canary-{tag}")
    other = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, status="published", store_root=tmp_path)
    for i, c in enumerate(chunk_document(doc, emb.count_tokens)):
        chunk = Chunk(
            tenant_id=b.id, document_version_id=other.version.id, document_id=other.document.id,
            subject_id=other.document.subject_id, ordinal=i, content=c.content, heading_path=c.heading_path,
            content_sha256=c.content_sha256, token_count=c.token_count, script=c.script,
        )
        raw_db.add(chunk)
        await raw_db.flush()
        raw_db.add(ChunkEmbedding(tenant_id=b.id, chunk_id=chunk.id, embedding_model=emb.model_id, embedding_dim=384, embedding=canary))
    await raw_db.commit()
    made = await queued_version(raw_db, a, tmp_path)
    assert await ingest.run_one(make_context(worker_dbs, tmp_path, slots), stub_stages(doc, emb)) == ingest.JobOutcome.DONE
    entries = (await raw_db.execute(select(AuditLog).where(AuditLog.action == "document:ingested", AuditLog.tenant_id == a.id))).scalars().all()
    (entry,) = [e for e in entries if e.details["version_id"] == str(made.version.id)]
    return canary, [v for _, _, v in await read_vectors(raw_db, made.version.id)], int(entry.details["vectors_reused"])


async def test_without_the_tenant_scoping_of_reuse_another_tenants_vector_is_copied(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = academic_world.a, academic_world.b
    canary, stored, reused = await _reuse_scenario(worker_dbs, a, b, raw_db, tmp_path, ingest_slots, "on")
    assert_reuse_is_tenant_scoped(canary, stored, reused)  # ON: passes

    async def unscoped(_db: AsyncSession, _tenant_id: Any, model_key: str, dim: int, hashes: list[str]) -> dict[tuple[str, str], str]:
        # a privileged session and no tenant filter at all: what the lookup would be without its two protections
        async with _TestSession() as everything:
            rows = (await everything.execute(
                text("SELECT DISTINCT ON (c.content_sha256, coalesce(c.heading_path, '')) c.content_sha256, coalesce(c.heading_path, ''), e.embedding::text "
                     "FROM app.chunks c JOIN app.chunk_embeddings e ON e.tenant_id = c.tenant_id AND e.chunk_id = c.id "
                     "WHERE e.embedding_model = :m AND e.embedding_dim = :d AND c.content_sha256 = ANY(CAST(:h AS varchar[]))"),
                {"m": model_key, "d": dim, "h": hashes})).all()
        return {(r[0], r[1]): r[2] for r in rows}

    monkeypatch.setattr(pipeline, "find_reusable_vectors", unscoped)  # protection OFF
    canary, stored, reused = await _reuse_scenario(worker_dbs, a, b, raw_db, tmp_path, ingest_slots, "off")
    with pytest.raises(AssertionError, match="another tenant|canary"):
        assert_reuse_is_tenant_scoped(canary, stored, reused)


# ── 5. The token-bound check ─────────────────────────────────────────────────


def _lumpy(text_: str) -> int:
    """Not additive: the more words in one text, the more tokens each costs (like a real sub-word tokenizer, only more)."""
    words = len(text_.split())
    return words + words * words // 400


def test_without_the_token_bound_check_chunks_overflow_the_model_and_the_helper_sees_it(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = flow_doc([("heading", "Mechanics", 1), ("para", " ".join(["momentum", "is", "conserved."] * 1500))])
    chunks = chunk_document(doc, _lumpy)
    assert chunks
    assert_chunks_within_model_limit(chunks, _lumpy)  # ON: passes

    def accept_everything(self: Any, cur: list[Any], _n_carry: int, _queue: Any, _heading: str, _target: int) -> tuple[str, int]:
        content = chunker._join(cur)
        return content, self.count(content)

    monkeypatch.setattr(chunker._Chunker, "_settle", accept_everything)  # protection OFF
    unchecked = chunk_document(doc, _lumpy)
    with pytest.raises(AssertionError, match="truncated|maximum"):
        assert_chunks_within_model_limit(unchecked, _lumpy)


# ── 6. The statement-count bound ─────────────────────────────────────────────


async def _statements(
    worker_dbs: WorkerDatabases, w: WorldTenant, raw_db: AsyncSession, tmp_path: Path, slots: SlotManager, n: int
) -> int:
    await queued_version(raw_db, w, tmp_path)
    counter = StatementCounter()
    detach = counter.attach(worker_dbs)
    try:
        st = stub_stages(slides_doc([f"slide {i} text" for i in range(n)]), RecordingEmbedder())
        st = replace(st, index=phased(counter, "index", st.index))
        assert await ingest.run_one(make_context(worker_dbs, tmp_path, slots), st) == ingest.JobOutcome.DONE
    finally:
        detach()
    return counter.counts["index"]


async def test_with_row_by_row_inserts_the_statement_count_grows_and_the_helper_sees_it(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    small = await _statements(worker_dbs, w, raw_db, tmp_path, ingest_slots, 20)
    large = await _statements(worker_dbs, w, raw_db, tmp_path, ingest_slots, 300)
    assert_statement_count_is_constant(small, large)  # ON: passes

    monkeypatch.setattr(pipeline, "BULK_INSERT_ROWS", 1)  # protection OFF: one row per statement
    small = await _statements(worker_dbs, w, raw_db, tmp_path, ingest_slots, 20)
    large = await _statements(worker_dbs, w, raw_db, tmp_path, ingest_slots, 300)
    with pytest.raises(AssertionError, match="statements"):
        assert_statement_count_is_constant(small, large)
