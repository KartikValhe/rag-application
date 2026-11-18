"""
End to end (step 5c-2): upload through the real API, run the worker loop for one job with the REAL stages
(sandboxed validate and parse child, chunker, embedding stage, index transaction) and the real queue, with the
FakeEmbedder, against the _test database. No model is loaded, no network is opened.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slots import SlotManager
from app.db.models.audit import AuditLog
from app.workers import ingest, pipeline, stages
from app.workers.db import WorkerDatabases
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth, make_document, upload
from tests.fixtures.parse_files import docx_bytes, make_encrypted_pdf, make_text_pdf, pptx_bytes
from tests.fixtures.pipeline import (
    DEFAULT_PIPELINE,
    RecordingEmbedder,
    SimulatedKill,
    count_rows,
    read_vectors,
)
from tests.fixtures.worker import job_row, make_context, version_row
from tests.security.upload_assertions import good_docx, make_pdf
from tests.test_ingest_sweeper import make_sweeper

pytestmark = pytest.mark.asyncio

PARAGRAPH = "Newton's second law states that force equals mass times acceleration. A heavier body needs more force."


def real_stages(embedder: RecordingEmbedder, store_root: Path, *, settings: pipeline.PipelineSettings = DEFAULT_PIPELINE) -> stages.IngestStages:
    from app.services import document_store

    return pipeline.build_ingest_stages(
        store=document_store.LocalPrivateStore(store_root), embedder=embedder, parse=stages.ParseSettings(
            timeout_seconds=60.0, max_output_bytes=14 * 1024 * 1024, memory_limit_mb=1024, cpu_seconds=60
        ),
        settings=settings,
    )


async def run_one_job(
    dbs: WorkerDatabases, store_root: Path, slots: SlotManager, embedder: RecordingEmbedder | None = None
) -> tuple[ingest.JobOutcome | None, RecordingEmbedder]:
    emb = embedder or RecordingEmbedder()
    ctx = make_context(dbs, store_root, slots)
    return await ingest.run_one(ctx, real_stages(emb, store_root)), emb


async def chunk_rows(db: AsyncSession, version_id: Any) -> list[dict[str, Any]]:
    rows = (await db.execute(text(
        "SELECT ordinal, page_start, page_end, heading_path, script, content FROM app.chunks WHERE document_version_id = :v ORDER BY ordinal"
    ), {"v": version_id})).mappings().all()
    return [dict(r) for r in rows]


async def upload_and_process(
    client: AsyncClient, w: WorldTenant, raw_db: AsyncSession, dbs: WorkerDatabases, store_root: Path, slots: SlotManager,
    *, data: bytes, filename: str, subject: str = "Math",
) -> tuple[Any, list[dict[str, Any]], RecordingEmbedder]:
    r = await upload(client, w, w.t1, subject=subject, data=data, filename=filename, title=filename)
    assert r.status_code == 202, r.text
    version_id = r.json()["id"]
    outcome, emb = await run_one_job(dbs, store_root, slots)
    assert outcome == ingest.JobOutcome.DONE, outcome
    v = await version_row(raw_db, version_id)
    assert (v.status, v.stage, v.error_code) == ("ready", None, None)
    chunks = await chunk_rows(raw_db, version_id)
    assert v.chunk_count == len(chunks) and len(chunks) >= 1
    assert await count_rows(raw_db, "chunk_embeddings", version_id) == len(chunks)
    return v, chunks, emb


# ── Each supported format, through the API and the worker ────────────────────


async def test_a_pdf_becomes_ready_with_page_numbers(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases,
    document_store_root: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    pdf = make_text_pdf([f"Page {n} of the syllabus. {PARAGRAPH}" for n in (1, 2, 3)])
    v, chunks, _ = await upload_and_process(client, academic_world.a, raw_db, worker_dbs, document_store_root, ingest_slots, data=pdf, filename="syllabus.pdf")
    assert v.page_count == 3
    assert chunks[0]["page_start"] == 1 and chunks[-1]["page_end"] == 3
    assert all(c["heading_path"] is None for c in chunks)  # PDFium gives lines, not headings
    assert "Page 2 of the syllabus." in " ".join(c["content"] for c in chunks)


async def test_a_docx_keeps_its_headings_and_has_no_page_numbers(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases,
    document_store_root: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    def build(d: Any) -> None:
        d.add_heading("Unit 1", level=1)
        d.add_paragraph(PARAGRAPH)
        d.add_heading("Motion", level=2)
        d.add_paragraph("Speed is distance divided by time.")
        d.add_heading("Unit 2", level=1)
        d.add_paragraph("Waves carry energy without carrying matter.")

    v, chunks, _ = await upload_and_process(client, academic_world.a, raw_db, worker_dbs, document_store_root, ingest_slots, data=docx_bytes(build), filename="notes.docx")
    assert [c["heading_path"] for c in chunks] == ["Unit 1", "Unit 1 > Motion", "Unit 2"]
    assert all(c["page_start"] is None and c["page_end"] is None for c in chunks)
    assert v.page_count == 1


async def test_a_pptx_has_one_chunk_per_slide_with_titles_and_notes(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases,
    document_store_root: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    def build(prs: Any) -> None:
        for title, body, note in (("Waves", "A wave carries energy.", "Show the rope demo."), ("Light", "Light is a wave.", "")):
            slide = prs.slides.add_slide(prs.slide_layouts[1])
            slide.shapes.title.text = title
            slide.placeholders[1].text = body
            if note:
                slide.notes_slide.notes_text_frame.text = note

    v, chunks, _ = await upload_and_process(client, academic_world.a, raw_db, worker_dbs, document_store_root, ingest_slots, data=pptx_bytes(build), filename="deck.pptx")
    assert [(c["heading_path"], c["page_start"], c["page_end"]) for c in chunks] == [("Waves", 1, 1), ("Light", 2, 2)]
    assert "Show the rope demo." in chunks[0]["content"]
    assert v.page_count == 2


async def test_txt_and_md_files(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases,
    document_store_root: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    txt = f"Unit 1: Algebra\n\n{PARAGRAPH}\n\nLinear equations in one variable.\n".encode()
    v, chunks, _ = await upload_and_process(client, w, raw_db, worker_dbs, document_store_root, ingest_slots, data=txt, filename="unit1.txt", subject="Math")
    assert chunks[0]["content"].startswith("Unit 1: Algebra\n") and chunks[0]["heading_path"] is None

    md = f"# Physics\n\n{PARAGRAPH}\n\n## Waves\n\nA wave carries energy.\n".encode()
    v, chunks, _ = await upload_and_process(client, w, raw_db, worker_dbs, document_store_root, ingest_slots, data=md, filename="physics.md", subject="Physics")
    assert [c["heading_path"] for c in chunks] == ["Physics", "Physics > Waves"]


async def test_a_hindi_docx_is_chunked_on_the_danda_and_marked_devanagari(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases,
    document_store_root: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    sentence = "भारत की राजधानी नई दिल्ली है।"

    def build(d: Any) -> None:
        d.add_heading("भारत का भूगोल", level=1)
        d.add_paragraph(" ".join([sentence] * 120))

    v, chunks, emb = await upload_and_process(client, academic_world.a, raw_db, worker_dbs, document_store_root, ingest_slots, data=docx_bytes(build), filename="hindi.docx")
    assert len(chunks) >= 2
    assert {c["script"] for c in chunks} == {"deva"} and {c["heading_path"] for c in chunks} == {"भारत का भूगोल"}
    assert all(c["content"].endswith("है।") for c in chunks)
    assert emb.embedded_texts[0].startswith("भारत का भूगोल\n")  # the heading path is part of the embedded text
    vectors = await read_vectors(raw_db, v.id)
    assert len(vectors) == len(chunks) and all(len(vec) == 384 for _, _, vec in vectors)


# ── Hostile and unusable files stay failed ───────────────────────────────────

GARBLED = "".join(chr(0xE000 + (i % 60)) for i in range(80)) + " Unit 1"

HOSTILE: list[tuple[str, str, bytes, str]] = [
    ("pdf_with_javascript", "pdf", make_pdf(b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >>"), "pdf_active_content"),
    ("macro_docx", "docx", good_docx({"word/vbaProject.bin": b"x"}), "macro_enabled_file"),
    ("executable_as_text", "txt", b"MZ\x90\x00 this is an executable", "disallowed_file_type"),
    ("encrypted_pdf", "pdf", make_encrypted_pdf(), "password_protected_pdf"),
    ("scanned_pdf", "pdf", make_text_pdf([None, None, None]), "scanned_pdf"),
    ("garbled_devanagari_pdf", "pdf", make_text_pdf([GARBLED], unicode_text=True), "unreadable_text"),
]


@pytest.mark.parametrize(("name", "kind", "data", "code"), HOSTILE, ids=[h[0] for h in HOSTILE])
async def test_hostile_and_unusable_files_fail_permanently_with_their_code_and_leave_no_chunks(
    name: str, kind: str, data: bytes, code: str, academic_world: AcademicWorld, raw_db: AsyncSession,
    worker_dbs: WorkerDatabases, tmp_path: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="queued", data=data, store_root=tmp_path)
    await raw_db.execute(text("UPDATE app.document_versions SET file_kind = :k WHERE id = :v"), {"k": kind, "v": made.version.id})
    await raw_db.commit()
    outcome, emb = await run_one_job(worker_dbs, tmp_path, ingest_slots)
    assert outcome == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", code)
    assert await count_rows(raw_db, "chunks", made.version.id) == 0
    assert emb.batches == []  # it never got as far as the model
    job = await job_row(raw_db, made.job.id)
    assert (job["status"], job["last_error_code"], job["attempts"]) == ("failed", code, 1)  # permanent: no retry


async def test_a_quota_breach_after_parsing_ends_failed_and_writes_nothing(
    academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    import json

    w = academic_world.a
    other = await make_document(raw_db, w, subject="Physics", classes=("10-A",), uploader=w.t1, status="published", data=b"already here", store_root=tmp_path, page_count=1)
    await raw_db.execute(text("UPDATE app.tenants SET doc_limits = CAST(:v AS jsonb) WHERE id = :id"), {"v": json.dumps({"max_pages": 1}), "id": w.id})
    await raw_db.commit()
    made = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="queued", data=PARAGRAPH.encode(), store_root=tmp_path)
    outcome, emb = await run_one_job(worker_dbs, tmp_path, ingest_slots)
    assert outcome == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", "quota_pages_exceeded")
    assert await count_rows(raw_db, "chunks", made.version.id) == 0 and emb.batches == []
    assert other.version.id != made.version.id


# ── A killed worker, two tenants at once, the delete cascade ─────────────────


async def test_a_worker_killed_after_embedding_is_recovered_by_the_sweeper_with_exactly_one_set_of_chunks(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases,
    document_store_root: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    r = await upload(client, w, w.t1, data=f"Unit 1: Algebra\n\n{PARAGRAPH}\n\nLinear equations.\n".encode(), filename="unit.txt")
    version_id = r.json()["id"]
    ctx = make_context(worker_dbs, document_store_root, ingest_slots)
    victim = real_stages(RecordingEmbedder(), document_store_root)
    real_embed = victim.embed

    async def die_after_embedding(sctx: stages.StageContext) -> None:
        await real_embed(sctx)
        raise SimulatedKill()

    from dataclasses import replace

    with pytest.raises(SimulatedKill):
        await ingest.run_one(ctx, replace(victim, embed=die_after_embedding))
    v = await version_row(raw_db, version_id)
    assert v.status == "processing" and await count_rows(raw_db, "chunks", version_id) == 0  # nothing half written

    await raw_db.execute(text("UPDATE app.ingest_jobs SET lease_expires_at = now() - interval '1 second' WHERE version_id = :v"), {"v": version_id})
    await raw_db.commit()
    assert (await make_sweeper(worker_dbs, document_store_root, only_tenants={w.id}).run_once()).failed_jobs == 0
    outcome, _ = await run_one_job(worker_dbs, document_store_root, ingest_slots)
    assert outcome == ingest.JobOutcome.DONE
    v = await version_row(raw_db, version_id)
    assert v.status == "ready"
    rows = await chunk_rows(raw_db, version_id)
    assert len(rows) == v.chunk_count and len({r_["ordinal"] for r_ in rows}) == len(rows)  # one set, no duplicates
    job = (await raw_db.execute(text("SELECT status, attempts FROM app.ingest_jobs WHERE version_id = :v"), {"v": version_id})).one()
    assert tuple(job) == ("done", 2)


async def test_two_tenants_processed_in_parallel_never_mix(
    academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases, tmp_path: Path,
    quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    a, b = academic_world.a, academic_world.b
    made: dict[str, Any] = {}
    for tag, w in (("ALPHA", a), ("BRAVO", b)):
        body = "\n\n".join(f"Paragraph {i} of tenant {tag}. {PARAGRAPH} {tag}-CANARY-{i}" for i in range(30)).encode()
        made[tag] = await make_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="queued", data=body, store_root=tmp_path)
    emb_a, emb_b = RecordingEmbedder(delay=0.05), RecordingEmbedder(delay=0.05)
    ctx_a, ctx_b = make_context(worker_dbs, tmp_path, ingest_slots), make_context(worker_dbs, tmp_path, ingest_slots)
    results = await asyncio.gather(
        ingest.run_one(ctx_a, real_stages(emb_a, tmp_path)), ingest.run_one(ctx_b, real_stages(emb_b, tmp_path))
    )
    assert results == [ingest.JobOutcome.DONE, ingest.JobOutcome.DONE]
    for tag, other_tag, w in (("ALPHA", "BRAVO", a), ("BRAVO", "ALPHA", b)):
        rows = (await raw_db.execute(text("SELECT tenant_id, content FROM app.chunks WHERE document_version_id = :v"), {"v": made[tag].version.id})).all()
        assert rows and all(r_[0] == w.id for r_ in rows)
        joined = " ".join(r_[1] for r_ in rows)
        assert f"{tag}-CANARY-0" in joined and f"{other_tag}-CANARY" not in joined
        emb_rows = (await raw_db.execute(text("SELECT DISTINCT e.tenant_id FROM app.chunk_embeddings e JOIN app.chunks c ON c.tenant_id = e.tenant_id AND c.id = e.chunk_id WHERE c.document_version_id = :v"), {"v": made[tag].version.id})).all()
        assert [r_[0] for r_ in emb_rows] == [w.id]


async def test_publish_then_delete_through_the_real_pipeline_removes_chunks_and_queues_the_files(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, worker_dbs: WorkerDatabases,
    document_store_root: Path, quiet_queue: None, ingest_slots: SlotManager,
) -> None:
    w = academic_world.a
    r = await upload(client, w, w.t1, data=f"Unit 1: Algebra\n\n{PARAGRAPH}\n".encode(), filename="unit.txt")
    doc_id, version_id = r.json()["document_id"], r.json()["id"]
    outcome, _ = await run_one_job(worker_dbs, document_store_root, ingest_slots)
    assert outcome == ingest.JobOutcome.DONE
    r = await client.post(f"/api/v1/documents/{doc_id}/versions/{version_id}/publish", headers=auth(w, w.t1), json={})
    assert r.status_code == 200, r.text
    assert await count_rows(raw_db, "chunks", version_id) >= 1

    r = await client.delete(f"/api/v1/documents/{doc_id}", headers=auth(w, w.t1))
    assert r.status_code == 204
    assert await count_rows(raw_db, "chunks", version_id) == 0 and await count_rows(raw_db, "chunk_embeddings", version_id) == 0
    outbox = (await raw_db.execute(text("SELECT storage_key, reason FROM app.storage_deletions WHERE tenant_id = :t"), {"t": w.id})).all()
    assert [o[1] for o in outbox] == ["document_deleted"]
    key = outbox[0][0]
    from app.services import document_store

    store = document_store.LocalPrivateStore(document_store_root)
    assert await store.get(w.id, key) is not None  # still there until the sweeper runs
    await make_sweeper(worker_dbs, document_store_root, only_tenants={w.id}).run_once()
    assert await store.get(w.id, key) is None
    r = await client.get(f"/api/v1/documents/{doc_id}/versions/{version_id}/download", headers=auth(w, w.t1))
    assert r.status_code == 404
    audit = {a.action for a in (await raw_db.execute(select(AuditLog).where(AuditLog.tenant_id == w.id))).scalars()}
    assert {"document:ingested", "document:publish", "document:delete"} <= audit
