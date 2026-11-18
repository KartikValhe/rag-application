"""
tests/fixtures/pipeline.py
──────────────────────────
Helpers for the real chunk / embed / index stages (step 5c-2):

  - `RecordingEmbedder`: a FakeEmbedder that records its batches, can be slow, and can fail or "die" on a batch.
  - `stub_stages`: the REAL chunk, embed and index stages with a stubbed validate and parse (the parsed
    document is given by the test), so these tests need no child process.
  - `claim_and_begin`: claim the job of a queued version and open it the way the worker does, returning the
    StageContext that stages receive (for tests that call one stage directly).
  - `StatementCounter`: counts the statements that run on the worker's tenant engine, by phase.
  - `slides_doc`, `add_version`, `read_vectors`, `ready_version`: data helpers.
"""

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document import Chunk, ChunkEmbedding, DocumentVersion, IngestJob
from app.ml.embedder import FakeEmbedder
from app.services import document_store
from app.services.chunker import ChunkingConfig
from app.services.parsed_document import ParsedDocument
from app.workers import ingest, pipeline, stages
from app.workers.db import WorkerDatabases
from tests.fixtures.academic_world import WorldTenant, fake_vector
from tests.fixtures.chunk_docs import build_doc
from tests.fixtures.documents import MadeDocument
from tests.fixtures.worker import FAST, PARSE, make_context

DEFAULT_PIPELINE = pipeline.PipelineSettings(
    chunking=ChunkingConfig(),
    embedding_batch_size=8,
    auto_publish=False,
    archive_retention_days=30,
    max_archived_versions=5,
)


class RecordingEmbedder(FakeEmbedder):
    """A FakeEmbedder that records each call (the batches) and can sleep, fail or 'die' on a batch."""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail_on_batch: int | None = None,
        die_on_batch: int | None = None,
        on_batch: Callable[[list[str]], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.delay = delay
        self.fail_on_batch = fail_on_batch
        self.die_on_batch = die_on_batch
        self.on_batch = on_batch
        self.batches: list[list[str]] = []

    def embed_passages(self, texts: Any) -> list[list[float]]:
        self.batches.append(list(texts))
        n = len(self.batches)
        if self.on_batch is not None:
            self.on_batch(list(texts))
        if self.die_on_batch == n:
            raise SimulatedKill()
        if self.fail_on_batch == n:
            raise RuntimeError("the embedder broke")
        if self.delay:
            time.sleep(self.delay)  # in the executor thread: the event loop (and the heartbeat) must keep going
        return super().embed_passages(texts)

    @property
    def embedded_texts(self) -> list[str]:
        return [t for batch in self.batches for t in batch]


class SimulatedKill(BaseException):  # noqa: N818  (a kill -9: nothing in the worker may catch it)
    pass


def slides_doc(texts: list[str], *, titles: list[str] | None = None) -> ParsedDocument:
    """A PPTX with one slide per text and a slide title each: one chunk per slide."""
    pages = [
        (i + 1, [("slide_title", titles[i] if titles else f"Slide {i + 1}"), ("para", t)]) for i, t in enumerate(texts)
    ]
    return build_doc("pptx", pages)


def stub_stages(
    doc: ParsedDocument,
    embedder: FakeEmbedder,
    *,
    settings: pipeline.PipelineSettings = DEFAULT_PIPELINE,
    store: document_store.PrivateObjectStore | None = None,
    executor: Any = None,
) -> stages.IngestStages:
    """The real chunk, embed and index stages; validate does nothing and parse hands over `doc`."""
    real = pipeline.build_ingest_stages(
        store=store or document_store.LocalPrivateStore(Path("unused-store-root")),
        embedder=embedder,
        parse=PARSE,
        settings=settings,
        executor=executor,
    )

    async def validate(_ctx: stages.StageContext) -> None:
        return None

    async def parse(ctx: stages.StageContext) -> None:
        ctx.results["parsed"] = doc

    return replace(real, validate=validate, parse=parse)


async def claim_and_begin(
    dbs: WorkerDatabases, store_root: Path, slots: Any, *, config: ingest.IngestSettings = FAST
) -> tuple[stages.StageContext, ingest.WorkerContext]:
    """Claim the next job and open it as the worker does (version -> processing). For direct stage calls."""
    wctx = make_context(dbs, store_root, slots, config=config)
    job = await wctx.queue.claim(wctx.worker_id)
    assert job is not None, "nothing to claim"
    info, limits = await ingest._begin(wctx, job)
    sctx = stages.StageContext(
        job=job,
        version=info,
        limits=limits,
        config=config,
        store=wctx.store,
        tenant_sessions=dbs.tenant_sessions,
        _session=lambda: stages.active_version_session(dbs.tenant_sessions, job),
    )
    return sctx, wctx


@dataclass
class StatementCounter:
    """Counts the statements that run on an engine, under a label the test sets."""

    counts: dict[str, int] = field(default_factory=dict)
    statements: dict[str, list[str]] = field(default_factory=dict)
    label: str = "other"

    def attach(self, dbs: WorkerDatabases) -> Callable[[], None]:
        sync = dbs.tenant_engine.sync_engine

        def before(_conn: Any, _cursor: Any, statement: str, _params: Any, _ctx: Any, _many: Any) -> None:
            self.counts[self.label] = self.counts.get(self.label, 0) + 1
            self.statements.setdefault(self.label, []).append(statement)

        event.listen(sync, "before_cursor_execute", before)
        return lambda: event.remove(sync, "before_cursor_execute", before)


def phased(counter: StatementCounter, name: str, stage: stages.Stage) -> stages.Stage:
    """Wrap a stage so its statements are counted under `name`."""

    async def run(ctx: stages.StageContext) -> None:
        counter.label = name
        try:
            await stage(ctx)
        finally:
            counter.label = "other"

    return run


async def add_version(
    db: AsyncSession, made: MadeDocument, w: WorldTenant, store_root: Path, *, data: bytes | None = None,
    status: str = "queued", page_count: int | None = None, version_no: int | None = None,
) -> MadeDocument:
    """A further version of an existing document (same uploader), with its stored file and its job."""
    body = data if data is not None else uuid.uuid4().hex.encode()
    key = document_store.new_document_key(w.id)
    next_no = version_no or (
        int((await db.execute(text("SELECT max(version_no) FROM app.document_versions WHERE document_id = :d"), {"d": made.document.id})).scalar_one()) + 1
    )
    published = status == "published"
    version = DocumentVersion(
        tenant_id=w.id,
        document_id=made.document.id,
        version_no=next_no,
        status=status,
        file_kind="txt",
        original_filename="notes.txt",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        storage_key=key,
        page_count=page_count,
        uploaded_by=made.version.uploaded_by,
        published_at=datetime.now(UTC) if published else None,
        published_by=made.version.uploaded_by if published else None,
    )
    db.add(version)
    await db.flush()
    job = IngestJob(
        tenant_id=w.id, version_id=version.id,
        status={"queued": "queued", "processing": "queued"}.get(status, "done"),
    )
    db.add(job)
    await db.commit()
    await document_store.LocalPrivateStore(store_root).put(w.id, key, body)
    return MadeDocument(made.document, version, job)


async def ready_version(
    db: AsyncSession, made: MadeDocument, w: WorldTenant, store_root: Path, *, status: str = "ready", chunks: int = 2,
    model: str = "fake-hash-384", flagged: int = 0, version_no: int | None = None,
) -> MadeDocument:
    """A version in `status` (ready, published or archived) WITH chunks and vectors, written directly."""
    out = await add_version(db, made, w, store_root, status=status, page_count=chunks, version_no=version_no)
    v = out.version
    for i in range(chunks):
        body = f"canary {v.id} chunk {i}"
        chunk = Chunk(
            tenant_id=w.id, document_version_id=v.id, document_id=v.document_id, subject_id=made.document.subject_id,
            ordinal=i, page_start=i + 1, page_end=i + 1, heading_path="Unit", content=body,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(), token_count=4, script="latn",
        )
        db.add(chunk)
        await db.flush()
        db.add(ChunkEmbedding(tenant_id=w.id, chunk_id=chunk.id, embedding_model=model, embedding_dim=384, embedding=fake_vector(body)))
    await db.execute(
        text("UPDATE app.document_versions SET chunk_count = :c, flagged_chunk_count = :f, embedding_model = :m WHERE id = :v"),
        {"c": chunks, "f": flagged, "m": model, "v": v.id},
    )
    await db.commit()
    return out


async def read_vectors(db: AsyncSession, version_id: uuid.UUID) -> list[tuple[int, str, list[float]]]:
    """(ordinal, content, vector) of a version's chunks, in ordinal order."""
    rows = (
        await db.execute(
            text(
                "SELECT c.ordinal, c.content, e.embedding::text FROM app.chunks c "
                "JOIN app.chunk_embeddings e ON e.tenant_id = c.tenant_id AND e.chunk_id = c.id "
                "WHERE c.document_version_id = :v ORDER BY c.ordinal"
            ),
            {"v": version_id},
        )
    ).all()
    return [(r[0], r[1], [float(x) for x in r[2].strip("[]").split(",")]) for r in rows]


async def count_rows(db: AsyncSession, table: str, version_id: uuid.UUID) -> int:
    if table == "chunk_embeddings":
        sql = (
            "SELECT count(*) FROM app.chunk_embeddings e JOIN app.chunks c ON c.tenant_id = e.tenant_id AND c.id = e.chunk_id "
            "WHERE c.document_version_id = :v"
        )
    else:
        sql = "SELECT count(*) FROM app.chunks WHERE document_version_id = :v"
    return int((await db.execute(text(sql), {"v": version_id})).scalar_one())


async def prepare_for_index(
    dbs: WorkerDatabases, w: WorldTenant, db: AsyncSession, store_root: Path, slots: Any, *, n: int = 6,
    doc: ParsedDocument | None = None, settings: pipeline.PipelineSettings = DEFAULT_PIPELINE,
) -> tuple[stages.StageContext, stages.IngestStages, RecordingEmbedder]:
    """A queued version, claimed, with the parse, chunk and embed stages already run: ready for the index stage."""
    from tests.fixtures.worker import queued_version

    await queued_version(db, w, store_root)
    emb = RecordingEmbedder()
    st = stub_stages(doc or slides_doc([f"slide text number {i} about waves" for i in range(n)]), emb, settings=settings)
    sctx, _ = await claim_and_begin(dbs, store_root, slots)
    await st.parse(sctx)
    await st.chunk(sctx)
    await st.embed(sctx)
    return sctx, st, emb
