"""
app/workers/pipeline.py
───────────────────────
The real ingestion stages of step 5c-2 and the factory that assembles them (design A3 steps 3-8):

    validate (sandbox) -> parse (sandbox) -> chunk -> embed -> index

`build_ingest_stages(store=..., embedder=..., parse=..., settings=...)` is the ONLY way the stages are built.
The worker's entry point passes the embedder it loaded once at start-up; tests pass the FakeEmbedder. There is
no environment variable, setting or import that selects a fake: the entry point cannot start without the
verified model files.

chunk   pure (services/chunker.py), run in a worker thread so a large document cannot stall the heartbeat.
        Nothing parsed (no chunks) is the permanent `no_text_found`.
embed   reuses a stored vector when this TENANT already has one for the same content hash, heading path and
        embedding model (one lookup statement per 1,000 hashes, through the tenant session, so RLS applies on top
        of the explicit tenant filter); computes the rest in batches of EMBEDDING_BATCH_SIZE on a thread, so the
        blocking ONNX call never freezes the event loop and the heartbeat keeps ticking; renews the lease after
        every batch (ctx.beat). No database transaction is open while the model runs. Nothing is written here:
        a crash anywhere in this stage leaves the database untouched.
index   ONE short transaction (design A3 step 8). In order: the document row is locked and the version and job
        row are checked to be this worker's (stages.active_version_session); the pages quota is re-checked under
        the tenant's quota lock (a breach is the permanent `quota_pages_exceeded` and rolls everything back; it
        comes BEFORE the writes so nothing is written and then thrown away); the version's chunks are deleted
        (idempotent re-run); chunks and vectors are inserted with a few multi-row INSERT ... SELECT FROM
        unnest(arrays) statements (BULK_INSERT_ROWS rows at most per statement, vectors as text cast to the
        pgvector type found through pg_extension, schema-qualified); the version becomes `ready` with page_count,
        chunk_count, embedding_model and flagged_chunk_count; the job becomes `done`; document:ingested is
        audited with the system actor; and, when DOC_AUTO_PUBLISH_ENABLED is on and nothing is flagged, the SAME
        publish function the route uses runs in this same transaction. Statements grow with batches, not chunks.

Injection flags (5d-2): BEFORE the index transaction opens, services/injection_scan.scan_chunks runs in a worker
thread (pure; it reads the chunks and never changes them). Each chunk's codes travel inside the existing chunk
INSERT (one comma-joined text per chunk, split again with string_to_array), so the statement count does not
change, and flagged_chunk_count is the number of chunks with at least one code. The scanner is a tripwire, not a
guarantee: auto-publish of an unflagged version means "nothing was detected", which is why
DOC_AUTO_PUBLISH_ENABLED still defaults to false (plan Q5). The document title and the file name are not
judged here: a title can change later, so the API scans both when it shows them.
"""

import asyncio
import logging
import math
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.embedder import Embedder
from app.services import chunker, document_lifecycle, document_store, injection_scan
from app.services.audit import record_audit_event
from app.services.chunker import ChunkingConfig, TextChunk
from app.workers import errors
from app.workers import stages as stages_module
from app.workers.stages import JOB_COMPLETED_KEY, IngestStages, ParseSettings, Stage, StageContext

if TYPE_CHECKING:
    from app.core.config import Settings

logger = logging.getLogger(__name__)

# Rows per bulk INSERT statement. A 1,000-chunk document is two statements (chunks, vectors). A test lowers it
# to 1 to prove the statement-count bound.
BULK_INSERT_ROWS = 2000
# Content hashes per reuse lookup statement
REUSE_LOOKUP_HASHES = 1000

RESULT_CHUNKS = "chunks"
RESULT_VECTORS = "vectors"  # list[str], pgvector text, aligned with the chunks
RESULT_REUSED = "vectors_reused"
RESULT_COMPUTED = "vectors_computed"
RESULT_FLAGGED = "flagged_chunk_count"
RESULT_FLAGS = "injection_flags"  # list[tuple[str, ...]], aligned with the chunks


@dataclass(frozen=True)
class PipelineSettings:
    chunking: ChunkingConfig
    embedding_batch_size: int
    auto_publish: bool
    archive_retention_days: int
    max_archived_versions: int

    @classmethod
    def from_settings(cls, s: "Settings") -> "PipelineSettings":
        return cls(
            chunking=ChunkingConfig(s.CHUNK_TARGET_TOKENS, s.CHUNK_MAX_TOKENS, s.CHUNK_OVERLAP_TOKENS),
            embedding_batch_size=s.EMBEDDING_BATCH_SIZE,
            auto_publish=s.DOC_AUTO_PUBLISH_ENABLED,
            archive_retention_days=s.DOC_ARCHIVE_FILE_RETENTION_DAYS,
            max_archived_versions=s.DOC_MAX_ARCHIVED_VERSIONS,
        )


def configuration_problems(s: "Settings") -> list[str]:
    """What stops the worker from starting (empty when it may start). Never reads a secret's value."""
    problems: list[str] = []
    if not s.WORKER_DATABASE_URL.strip():
        problems.append("WORKER_DATABASE_URL is not set (the worker connects to the database as the app_worker role)")
    if not s.REDIS_URL.strip():
        problems.append("REDIS_URL is not set (the worker's job slots and leases live in Redis)")
    if not s.DOCUMENT_STORE_DIR.strip():
        problems.append("DOCUMENT_STORE_DIR is not set (the worker reads the uploaded originals from the document store)")
    if not s.EMBEDDING_MODEL_DIR.strip():
        problems.append(
            "EMBEDDING_MODEL_DIR is not set (python -m app.scripts.fetch_model e5-small --out <dir>, then point "
            "EMBEDDING_MODEL_DIR at it)"
        )
    return problems


# ── Vectors as text ──────────────────────────────────────────────────────────


def format_vector(vector: Sequence[float]) -> str:
    """pgvector's text form: '[0.1,0.2,...]'."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


REUSE_SQL = """
SELECT DISTINCT ON (c.content_sha256, coalesce(c.heading_path, ''))
       c.content_sha256, coalesce(c.heading_path, ''), e.embedding::text
FROM app.chunks c
JOIN app.chunk_embeddings e ON e.tenant_id = c.tenant_id AND e.chunk_id = c.id
WHERE c.tenant_id = :t AND e.tenant_id = :t
  AND e.embedding_model = :m AND e.embedding_dim = :d
  AND c.content_sha256 = ANY(CAST(:h AS varchar[]))
"""


async def find_reusable_vectors(
    db: AsyncSession, tenant_id: uuid.UUID, model_key: str, dim: int, hashes: Sequence[str]
) -> dict[tuple[str, str], str]:
    """
    Stored vectors of THIS tenant for the embedding model `model_key`, by (content hash, heading path), as pgvector
    text. The embedded text is the heading path plus the content, so the heading is part of the key. Tenant
    scoping is double: the explicit filter and the session's RLS (app.tenant_id).
    """
    rows = (await db.execute(text(REUSE_SQL), {"t": tenant_id, "m": model_key, "d": dim, "h": list(hashes)})).all()
    return {(r[0], r[1]): r[2] for r in rows}


# ── chunk ────────────────────────────────────────────────────────────────────


def make_chunk_stage(embedder: Embedder, config: ChunkingConfig) -> Stage:
    async def chunk(ctx: StageContext) -> None:
        parsed = ctx.results["parsed"]
        chunks = await asyncio.to_thread(chunker.chunk_document, parsed, embedder.count_tokens, config)
        if not chunks:
            raise errors.PermanentIngestError("no_text_found")  # headings only, or nothing left after cleaning
        ctx.results[RESULT_CHUNKS] = chunks

    return chunk


# ── embed ────────────────────────────────────────────────────────────────────


def make_embed_stage(embedder: Embedder, batch_size: int, executor: ThreadPoolExecutor) -> Stage:
    async def embed(ctx: StageContext) -> None:
        chunks: list[TextChunk] = ctx.results[RESULT_CHUNKS]
        model_key, dim = embedder.model_id, embedder.dim
        truncated_before = int(getattr(embedder, "truncated_count", 0))

        hashes = list(dict.fromkeys(c.content_sha256 for c in chunks))
        reusable: dict[tuple[str, str], str] = {}
        async with ctx.session() as db:  # short: the lookups only; closed before the model runs
            for start in range(0, len(hashes), REUSE_LOOKUP_HASHES):
                reusable.update(
                    await find_reusable_vectors(
                        db, ctx.job.tenant_id, model_key, dim, hashes[start : start + REUSE_LOOKUP_HASHES]
                    )
                )

        vectors: list[str | None] = [reusable.get((c.content_sha256, c.heading_path)) for c in chunks]
        reused = sum(1 for v in vectors if v is not None)
        # Texts still to compute; the same text twice in one document is computed once
        first_of: dict[str, int] = {}
        for i, vec in enumerate(vectors):
            if vec is None:
                first_of.setdefault(chunks[i].embedding_text, i)
        todo = list(first_of.values())

        loop = asyncio.get_running_loop()
        computed: dict[str, str] = {}
        for start in range(0, len(todo), batch_size):
            batch = todo[start : start + batch_size]
            texts = [chunks[i].embedding_text for i in batch]
            out = await loop.run_in_executor(executor, embedder.embed_passages, texts)
            if len(out) != len(texts) or any(len(v) != dim or not all(math.isfinite(x) for x in v) for v in out):
                raise RuntimeError("the embedder returned a vector of the wrong count, size or value")
            for text_, vector in zip(texts, out, strict=True):
                computed[text_] = format_vector(vector)
            await ctx.beat()  # after EVERY batch: renew the lease and both slots; JobStopped if the job is lost

        final: list[str] = []
        for i, vec in enumerate(vectors):
            final.append(vec if vec is not None else computed[chunks[i].embedding_text])
        ctx.results[RESULT_VECTORS] = final
        ctx.results[RESULT_REUSED] = reused
        ctx.results[RESULT_COMPUTED] = len(todo)
        truncated = int(getattr(embedder, "truncated_count", 0)) - truncated_before
        logger.info(
            "embedding_done version=%s chunks=%d reused=%d computed=%d truncated=%d",
            ctx.version.id, len(chunks), reused, len(todo), truncated,  # counts only: no text, no file name
        )
        ctx.results["truncated_texts"] = truncated

    return embed


# ── index ────────────────────────────────────────────────────────────────────

VECTOR_SCHEMA_SQL = (
    "SELECT format('%I', n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
    "WHERE e.extname = 'vector'"
)

INSERT_CHUNKS_SQL = """
INSERT INTO app.chunks (tenant_id, id, document_version_id, document_id, subject_id, ordinal, page_start, page_end,
                        heading_path, content, content_sha256, token_count, script, injection_flags)
SELECT CAST(:t AS uuid), u.id, CAST(:v AS uuid), CAST(:d AS uuid), CAST(:s AS uuid), u.ordinal, u.page_start, u.page_end,
       NULLIF(u.heading_path, ''), u.content, u.sha, u.tokens, u.script,
       CAST(string_to_array(u.flags, ',') AS text[])
FROM unnest(CAST(:ids AS uuid[]), CAST(:ordinals AS integer[]), CAST(:page_starts AS integer[]),
            CAST(:page_ends AS integer[]), CAST(:headings AS text[]), CAST(:contents AS text[]),
            CAST(:shas AS text[]), CAST(:tokens AS integer[]), CAST(:scripts AS text[]), CAST(:flags AS text[]))
     AS u(id, ordinal, page_start, page_end, heading_path, content, sha, tokens, script, flags)
"""

# {schema} is the quoted schema of the vector extension, read from pg_extension (never hardcoded)
INSERT_EMBEDDINGS_SQL = """
INSERT INTO app.chunk_embeddings (tenant_id, chunk_id, embedding_model, embedding_dim, embedding)
SELECT CAST(:t AS uuid), u.id, CAST(:m AS varchar), CAST(:dim AS smallint), CAST(u.vec AS {schema}.vector)
FROM unnest(CAST(:ids AS uuid[]), CAST(:vecs AS text[])) AS u(id, vec)
"""

READY_SQL = """
UPDATE app.document_versions
SET status = 'ready', stage = NULL, error_code = NULL, page_count = :pc, chunk_count = :cc,
    flagged_chunk_count = :fc, embedding_model = :m, updated_at = now()
WHERE tenant_id = :t AND id = :v AND status IN ('queued', 'processing')
RETURNING version_no, uploaded_by
"""

# The job is locked FOR SHARE by this transaction (stages.lock_job_ownership), so the row cannot change under us.
JOB_DONE_SQL = """
UPDATE app.ingest_jobs SET status = 'done', last_error_code = NULL, updated_at = now()
WHERE tenant_id = :t AND version_id = :v AND status = 'running'
"""


async def write_index(
    db: AsyncSession, ctx: StageContext, settings: PipelineSettings, embedder: Embedder
) -> dict[str, Any]:
    """The body of the index transaction (see the module docstring). Returns the numbers for the audit entry."""
    chunks: list[TextChunk] = ctx.results[RESULT_CHUNKS]
    vectors: list[str] = ctx.results[RESULT_VECTORS]
    parsed = ctx.results["parsed"]
    tenant, version = ctx.job.tenant_id, ctx.version
    flags: list[tuple[str, ...]] = ctx.results[RESULT_FLAGS]
    if len(vectors) != len(chunks):
        raise RuntimeError("the embed stage did not produce one vector per chunk")
    if len(flags) != len(chunks):
        raise RuntimeError("the scanner did not produce one set of flags per chunk")
    if any("," in code for codes in flags for code in codes):
        raise RuntimeError("an injection code contains a comma")  # they are joined with commas below
    flagged = sum(1 for codes in flags if codes)

    schema = (await db.execute(text(VECTOR_SCHEMA_SQL))).scalar_one_or_none()
    if schema is None:
        raise RuntimeError("the pgvector extension is not installed in this database")

    # (d) the binding pages quota check, under the tenant's quota lock, before anything is written
    from app.services.document_upload import lock_quota

    await lock_quota(db, tenant)
    used = await stages_module.pages_used_by_others(db, tenant, version.id)
    stages_module.check_pages_quota(ctx.limits, used_pages=used, new_pages=parsed.page_count)

    # (b) replace what an earlier run may have left (idempotent re-run), (c) bulk insert
    await db.execute(
        text("DELETE FROM app.chunks WHERE tenant_id = :t AND document_version_id = :v"), {"t": tenant, "v": version.id}
    )
    ids = [uuid.uuid4() for _ in chunks]
    step = max(1, BULK_INSERT_ROWS)
    for start in range(0, len(chunks), step):
        part = chunks[start : start + step]
        await db.execute(
            text(INSERT_CHUNKS_SQL),
            {
                "t": tenant, "v": version.id, "d": version.document_id, "s": version.subject_id,
                "ids": ids[start : start + step],
                "ordinals": [c.ordinal for c in part],
                "page_starts": [c.page_start for c in part],
                "page_ends": [c.page_end for c in part],
                "headings": [c.heading_path for c in part],
                "contents": [c.content for c in part],
                "shas": [c.content_sha256 for c in part],
                "tokens": [c.token_count for c in part],
                "scripts": [c.script for c in part],
                "flags": [",".join(codes) for codes in flags[start : start + step]],
            },
        )
    for start in range(0, len(chunks), step):
        await db.execute(
            text(INSERT_EMBEDDINGS_SQL.format(schema=schema)),
            {
                "t": tenant, "m": embedder.model_id, "dim": embedder.dim,
                "ids": ids[start : start + step], "vecs": vectors[start : start + step],
            },
        )

    # (e) the version is ready, (f) the job is done
    ready = (
        await db.execute(
            text(READY_SQL),
            {"pc": parsed.page_count, "cc": len(chunks), "fc": flagged, "m": embedder.model_id, "t": tenant, "v": version.id},
        )
    ).first()
    if ready is None:
        raise errors.JobStopped("cancelled")  # no longer queued or processing (deleted meanwhile)
    version_no, uploaded_by = int(ready[0]), ready[1]
    done = await db.execute(text(JOB_DONE_SQL), {"t": tenant, "v": version.id})
    if done.rowcount != 1:  # type: ignore[attr-defined]
        raise errors.JobStopped("job_lost")

    # Auto-publish: the same function as the route, in this same transaction
    auto_published = False
    if settings.auto_publish and flagged == 0:
        await document_lifecycle.publish_version(
            db,
            tenant_id=tenant,
            document_id=version.document_id,
            version_id=version.id,
            actor_id=uploaded_by,
            auto=True,
            retention_days=settings.archive_retention_days,
            max_archived=settings.max_archived_versions,
        )
        auto_published = True

    # (g) the system actor; ids, counts and the model key only, never file content
    details = {
        "version_id": str(version.id),
        "version_no": version_no,
        "chunk_count": len(chunks),
        "page_count": parsed.page_count,
        "vectors_reused": int(ctx.results.get(RESULT_REUSED, 0)),
        "vectors_computed": int(ctx.results.get(RESULT_COMPUTED, 0)),
        "embedding_model": embedder.model_id,
        "auto_published": auto_published,
        "flagged_chunk_count": flagged,
    }
    await record_audit_event(
        tenant_id=tenant, action="document:ingested", target_type="document", target_id=str(version.document_id),
        details=details, actor_id=None, db=db,
    )
    return details


def make_index_stage(embedder: Embedder, settings: PipelineSettings) -> Stage:
    async def index(ctx: StageContext) -> None:
        # The scan is pure and can take a moment on a large document: a worker thread, and no transaction open yet.
        chunks: list[TextChunk] = ctx.results[RESULT_CHUNKS]
        flags = await asyncio.to_thread(injection_scan.scan_chunks, chunks)
        ctx.results[RESULT_FLAGS] = flags
        ctx.results[RESULT_FLAGGED] = sum(1 for codes in flags if codes)
        # Document row first (see active_version_session), then the version and job rows: one lock order.
        async with stages_module.active_version_session(
            ctx.tenant_sessions, ctx.job, lock_document=ctx.version.document_id
        ) as db:
            await write_index(db, ctx, settings, embedder)
        # Committed: the version is ready (or published) and the job is done. The worker must not complete it again.
        ctx.results[JOB_COMPLETED_KEY] = True

    return index


# ── The factory ──────────────────────────────────────────────────────────────


def build_ingest_stages(
    *,
    store: document_store.PrivateObjectStore,
    embedder: Embedder,
    parse: ParseSettings,
    settings: PipelineSettings,
    executor: ThreadPoolExecutor | None = None,
) -> IngestStages:
    """
    The real stages. `embedder` is whatever the caller loaded: the worker entry point passes the verified model,
    tests pass the FakeEmbedder. `executor` runs the blocking model calls (default: one dedicated thread).
    """
    pool = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="embed")
    return IngestStages(
        validate=stages_module.make_validate_stage(store, parse),
        parse=stages_module.make_parse_stage(store, parse, pages_quota_hook=stages_module.make_pages_quota_hook()),
        chunk=make_chunk_stage(embedder, settings.chunking),
        embed=make_embed_stage(embedder, settings.embedding_batch_size, pool),
        index=make_index_stage(embedder, settings),
    )
