"""
The embedding stage (step 5c-2, design A3 step 7): batches, reuse of vectors by content hash within one
tenant, the blocking model call in an executor, a heartbeat after every batch, and nothing written (and no
database lock held) while the model runs.

The REAL chunk, embed and index stages run with a stubbed parse (tests/fixtures/pipeline.py); the queue, slots,
tenant sessions and Postgres are real; the embedder is the FakeEmbedder. The _test database only.
"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slots import SlotManager
from app.db.models.audit import AuditLog
from app.db.models.document import Chunk, ChunkEmbedding
from app.services.chunker import ChunkingConfig, chunk_document
from app.workers import ingest, stages
from app.workers.db import WorkerDatabases
from tests.fixtures.academic_world import AcademicWorld, fake_vector
from tests.fixtures.documents import make_document
from tests.fixtures.pipeline import (
    DEFAULT_PIPELINE,
    RecordingEmbedder,
    SimulatedKill,
    add_version,
    claim_and_begin,
    count_rows,
    read_vectors,
    slides_doc,
    stub_stages,
)
from tests.fixtures.worker import FAST, job_row, make_context, queued_version, version_row

pytestmark = pytest.mark.asyncio


async def run_job(
    dbs: WorkerDatabases, tmp_path: Path, slots: SlotManager, st: stages.IngestStages, *, config: ingest.IngestSettings = FAST
) -> ingest.JobOutcome | None:
    return await ingest.run_one(make_context(dbs, tmp_path, slots, config=config), st)


async def ingested_details(db: AsyncSession, version_id: object) -> dict[str, object]:
    rows = (await db.execute(select(AuditLog).where(AuditLog.action == "document:ingested"))).scalars().all()
    (entry,) = [r for r in rows if r.details.get("version_id") == str(version_id)]
    return dict(entry.details)


# ── Batches and order ────────────────────────────────────────────────────────


async def test_texts_are_embedded_in_batches_of_the_batch_size_in_chunk_order(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    doc = slides_doc([f"Slide body number {i} about motion and force." for i in range(20)])
    emb = RecordingEmbedder()
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(doc, emb)) == ingest.JobOutcome.DONE

    assert [len(b) for b in emb.batches] == [8, 8, 4]
    expected = [c.embedding_text for c in chunk_document(doc, emb.count_tokens)]
    assert emb.embedded_texts == expected  # all of them, once, in chunk order
    stored = await read_vectors(raw_db, made.version.id)
    assert [ordinal for ordinal, _, _ in stored] == list(range(20))
    for (_, _, vector), text_ in zip(stored, expected, strict=True):
        assert max(abs(a - b) for a, b in zip(vector, emb._vector(text_), strict=True)) < 1e-6
    details = await ingested_details(raw_db, made.version.id)
    assert (details["vectors_reused"], details["vectors_computed"]) == (0, 20)


async def test_the_batch_size_comes_from_the_pipeline_settings(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    await queued_version(raw_db, academic_world.a, tmp_path)
    emb = RecordingEmbedder()
    settings = replace(DEFAULT_PIPELINE, embedding_batch_size=3)
    doc = slides_doc([f"text {i}" for i in range(7)])
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(doc, emb, settings=settings)) == ingest.JobOutcome.DONE
    assert [len(b) for b in emb.batches] == [3, 3, 1]


# ── Reuse by content hash, in one tenant, for one model ──────────────────────


async def _finish_first_version(
    worker_dbs: WorkerDatabases, w: object, raw_db: AsyncSession, tmp_path: Path, slots: SlotManager, doc: object,
    emb: RecordingEmbedder,
) -> object:
    made = await queued_version(raw_db, w, tmp_path)  # type: ignore[arg-type]
    assert await run_job(worker_dbs, tmp_path, slots, stub_stages(doc, emb)) == ingest.JobOutcome.DONE  # type: ignore[arg-type]
    await raw_db.execute(
        text("UPDATE app.document_versions SET status = 'published', published_at = now(), published_by = uploaded_by WHERE id = :v"),
        {"v": made.version.id},
    )
    await raw_db.commit()
    return made


async def test_a_reuploaded_version_with_one_changed_page_reuses_the_other_vectors(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    texts = ["Newton first law about inertia.", "Newton second law about force and mass.", "Newton third law about reaction."]
    emb1 = RecordingEmbedder()
    made1 = await _finish_first_version(worker_dbs, w, raw_db, tmp_path, ingest_slots, slides_doc(texts), emb1)
    assert len(emb1.embedded_texts) == 3

    made2 = await add_version(raw_db, made1, w, tmp_path)  # type: ignore[arg-type]
    changed = [texts[0], "Newton second law, now with the formula F equals m a.", texts[2]]
    emb2 = RecordingEmbedder()
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(slides_doc(changed), emb2)) == ingest.JobOutcome.DONE
    assert emb2.embedded_texts == ["Slide 2\nNewton second law, now with the formula F equals m a."]  # only the changed page
    details = await ingested_details(raw_db, made2.version.id)
    assert (details["vectors_reused"], details["vectors_computed"], details["chunk_count"]) == (2, 1, 3)

    old = await read_vectors(raw_db, made1.version.id)  # type: ignore[attr-defined]
    new = await read_vectors(raw_db, made2.version.id)
    assert new[0][2] == old[0][2] and new[2][2] == old[2][2]  # the reused vectors are the stored ones
    assert new[1][2] != old[1][2]
    assert max(abs(a - b) for a, b in zip(new[1][2], emb2._vector(emb2.embedded_texts[0]), strict=True)) < 1e-6


async def test_a_tenant_never_reuses_another_tenants_vectors(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    a, b = academic_world.a, academic_world.b
    doc = slides_doc(["The same text in two tenants.", "Another shared line."])
    emb = RecordingEmbedder()
    # Tenant B already holds chunks with the very same hash and heading, and CANARY vectors, for the same model
    other = await make_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, status="published", store_root=tmp_path)
    canary = fake_vector("tenant-b-canary")
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
    claimed = await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(doc, emb))
    assert claimed == ingest.JobOutcome.DONE
    assert len(emb.embedded_texts) == 2  # nothing came from tenant B
    details = await ingested_details(raw_db, made.version.id)
    assert details["vectors_reused"] == 0
    for _, _, vector in await read_vectors(raw_db, made.version.id):
        assert max(abs(x - y) for x, y in zip(vector, canary, strict=True)) > 0.01  # not the canary


async def test_a_vector_of_another_model_or_another_heading_is_not_reused(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    texts = ["Alpha beta gamma.", "Delta epsilon zeta."]
    emb1 = RecordingEmbedder(model_id="model-one")
    made1 = await _finish_first_version(worker_dbs, w, raw_db, tmp_path, ingest_slots, slides_doc(texts), emb1)

    # Same texts, another model key: everything is computed again
    made2 = await add_version(raw_db, made1, w, tmp_path)  # type: ignore[arg-type]
    emb2 = RecordingEmbedder(model_id="model-two")
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(slides_doc(texts), emb2)) == ingest.JobOutcome.DONE
    assert len(emb2.embedded_texts) == 2
    await raw_db.execute(text("UPDATE app.document_versions SET status = 'archived' WHERE id = :v"), {"v": made2.version.id})
    await raw_db.execute(text("UPDATE app.document_versions SET status = 'published', published_at = now() WHERE id = :v"), {"v": made1.version.id})  # type: ignore[attr-defined]
    await raw_db.commit()

    # Same texts and model, other headings (the heading is part of the embedded text): not reused either
    made3 = await add_version(raw_db, made1, w, tmp_path)  # type: ignore[arg-type]
    emb3 = RecordingEmbedder(model_id="model-one")
    retitled = slides_doc(texts, titles=["Other A", "Other B"])
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(retitled, emb3)) == ingest.JobOutcome.DONE
    assert len(emb3.embedded_texts) == 2
    assert (await ingested_details(raw_db, made3.version.id))["vectors_reused"] == 0


# ── Nothing half written, no lock while the model runs ───────────────────────


@pytest.mark.parametrize("how", ["error", "kill"])
async def test_a_crash_after_batch_one_leaves_nothing_half_written(
    how: str, worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)
    doc = slides_doc([f"slide text {i}" for i in range(20)])
    emb = RecordingEmbedder(fail_on_batch=2) if how == "error" else RecordingEmbedder(die_on_batch=2)
    st = stub_stages(doc, emb)
    if how == "kill":
        with pytest.raises(SimulatedKill):
            await run_job(worker_dbs, tmp_path, ingest_slots, st)
        assert (await version_row(raw_db, made.version.id)).status == "processing"  # nobody released it: dead worker
    else:
        assert await run_job(worker_dbs, tmp_path, ingest_slots, st) == ingest.JobOutcome.RETRY  # a crash: requeued
        assert (await version_row(raw_db, made.version.id)).status == "queued"
    assert len(emb.batches) == 2  # batch 1 was computed, batch 2 broke
    assert await count_rows(raw_db, "chunks", made.version.id) == 0
    assert await count_rows(raw_db, "chunk_embeddings", made.version.id) == 0
    assert (await version_row(raw_db, made.version.id)).chunk_count is None


async def test_no_database_connection_is_held_while_the_model_runs(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """No tenant transaction (so no lock on the job or version row) is open during any model call."""
    await queued_version(raw_db, academic_world.a, tmp_path)
    seen: list[int] = []
    emb = RecordingEmbedder(on_batch=lambda _t: seen.append(worker_dbs.tenant_engine.pool.checkedout()))
    doc = slides_doc([f"text {i}" for i in range(20)])
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(doc, emb)) == ingest.JobOutcome.DONE
    assert seen == [0, 0, 0]


async def test_the_heartbeat_runs_after_every_batch_through_the_ownership_checked_machinery(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    await queued_version(raw_db, academic_world.a, tmp_path)
    emb = RecordingEmbedder()
    st = stub_stages(slides_doc([f"text {i}" for i in range(20)]), emb)
    sctx, _wctx = await claim_and_begin(worker_dbs, tmp_path, ingest_slots)
    beats: list[int] = []

    async def beat() -> None:
        beats.append(len(emb.batches))

    sctx.beat = beat
    sctx.results["parsed"] = slides_doc([f"text {i}" for i in range(20)])
    await st.chunk(sctx)
    await st.embed(sctx)
    assert beats == [1, 2, 3]  # once after each of the three batches, never before the first


async def test_a_lost_job_stops_the_embedding_between_batches(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """The beat after a batch raises JobStopped when the job is no longer this worker's: no more batches run."""
    await queued_version(raw_db, academic_world.a, tmp_path)
    emb = RecordingEmbedder()
    st = stub_stages(slides_doc([f"text {i}" for i in range(40)]), emb)
    sctx, _wctx = await claim_and_begin(worker_dbs, tmp_path, ingest_slots)

    async def beat() -> None:
        if len(emb.batches) == 2:
            raise ingest.errors.JobStopped("job_lost")

    sctx.beat = beat
    sctx.results["parsed"] = slides_doc([f"text {i}" for i in range(40)])
    await st.chunk(sctx)
    with pytest.raises(ingest.errors.JobStopped):
        await st.embed(sctx)
    assert len(emb.batches) == 2


async def test_a_slow_model_keeps_the_lease_alive_through_the_heartbeat_and_the_job_completes(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """The model call takes far longer than the lease. It runs in an executor, so the heartbeat keeps renewing;
    a second worker polling the queue the whole time never steals the job."""
    from tests.fixtures.worker import make_queue

    made = await queued_version(raw_db, academic_world.a, tmp_path)
    config = replace(FAST, lease_seconds=1.5, heartbeat_seconds=0.15, max_job_seconds=120.0)
    emb = RecordingEmbedder(delay=0.4)  # 5 batches x 0.4 s = 2 s of model time against a 1.5 s lease
    st = stub_stages(slides_doc([f"text {i}" for i in range(40)]), emb)
    runner = asyncio.create_task(run_job(worker_dbs, tmp_path, ingest_slots, st, config=config))
    thief_queue = make_queue(worker_dbs, config)
    stolen = []
    while not runner.done():
        got = await thief_queue.claim("thief")
        if got is not None:
            stolen.append(got)
        await asyncio.sleep(0.2)
    assert await runner == ingest.JobOutcome.DONE
    assert stolen == []
    assert len(emb.batches) == 5
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"]) == ("done", 1)
    assert (await version_row(raw_db, made.version.id)).status == "ready"


async def test_a_model_that_returns_the_wrong_shape_is_a_crash_not_a_corrupt_write(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    made = await queued_version(raw_db, academic_world.a, tmp_path)

    class ShortEmbedder(RecordingEmbedder):
        def embed_passages(self, texts: object) -> list[list[float]]:
            return super().embed_passages(texts)[:-1]  # one vector too few

    st = stub_stages(slides_doc([f"text {i}" for i in range(5)]), ShortEmbedder())
    assert await run_job(worker_dbs, tmp_path, ingest_slots, st) == ingest.JobOutcome.RETRY
    assert await count_rows(raw_db, "chunks", made.version.id) == 0


async def test_the_embedder_is_never_asked_for_an_empty_batch_and_empty_documents_fail_permanently(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    from tests.fixtures.chunk_docs import flow_doc

    made = await queued_version(raw_db, academic_world.a, tmp_path)
    emb = RecordingEmbedder()
    only_a_heading = flow_doc([("heading", "Only a title", 1)])
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(only_a_heading, emb)) == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", "no_text_found")
    assert emb.batches == []


async def test_the_chunk_stage_is_fed_the_configured_budget(
    worker_dbs: WorkerDatabases, academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    """CHUNK_* come from the pipeline settings: a smaller target gives more, smaller chunks."""
    from tests.fixtures.chunk_docs import flow_doc, sentence_text

    made = await queued_version(raw_db, academic_world.a, tmp_path)
    settings = replace(DEFAULT_PIPELINE, chunking=ChunkingConfig(target_tokens=100, max_tokens=120, overlap_tokens=10))
    emb = RecordingEmbedder()
    doc = flow_doc([("para", sentence_text(1000))], "txt")
    assert await run_job(worker_dbs, tmp_path, ingest_slots, stub_stages(doc, emb, settings=settings)) == ingest.JobOutcome.DONE
    rows = (await raw_db.execute(select(Chunk.token_count).where(Chunk.document_version_id == made.version.id))).scalars().all()
    assert len(rows) >= 9 and max(rows) <= 100
