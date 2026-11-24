"""
tests/fixtures/retrieval_world.py
─────────────────────────────────
Rows for the retrieval tests (step 5d-1): documents with several chunks, chosen embeddings and chosen
version states, on top of the academic world (tests/fixtures/academic_world.py). Everything is written
through the privileged (no-RLS) session; the searches themselves run as app_tenant.

  ScriptedEmbedder  maps chosen texts to chosen vectors, so a test can say exactly how similar the query
                    is to each chunk (used for the "RRF beats either side", "rare keyword" and
                    "paraphrase" cases). Every other text gets the zero-similarity fallback vector.
  axis_vector       a unit vector whose cosine with the query axis is exactly `sim`.
  seed_document     one document (or one more version of an existing document) with N chunks and their
                    embeddings.
"""

import hashlib
import math
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document import Chunk, ChunkEmbedding, Document, DocumentClass, DocumentVersion
from app.db.models.user import User
from app.ml.embedder import FakeEmbedder
from app.ml.model_keys import E5_SMALL_DIM, E5_SMALL_MODEL_KEY
from tests.fixtures.academic_world import WorldTenant

QUERY_AXIS = 0


def axis_vector(sim: float, axis: int, dim: int = E5_SMALL_DIM) -> list[float]:
    """A unit vector with cosine `sim` to the query axis; the rest of its length lies on its own `axis`."""
    if not 1 <= axis < dim:
        raise ValueError("axis must be between 1 and dim - 1")
    vec = [0.0] * dim
    vec[QUERY_AXIS] = sim
    vec[axis] = math.sqrt(max(0.0, 1.0 - sim * sim))
    return vec


def query_vector(dim: int = E5_SMALL_DIM) -> list[float]:
    vec = [0.0] * dim
    vec[QUERY_AXIS] = 1.0
    return vec


class ScriptedEmbedder:
    """An embedder that returns chosen vectors for chosen texts (NFC compared). Any other passage gets a
    vector orthogonal to the query axis, and any other query gets the query axis, so an unscripted
    passage has similarity 0 to any unscripted query."""

    def __init__(
        self,
        vectors: dict[str, list[float]],
        *,
        dim: int = E5_SMALL_DIM,
        model_id: str = E5_SMALL_MODEL_KEY,
        fallback_axis: int | None = None,
    ) -> None:
        self.dim = dim
        self.max_tokens = 512
        self.model_id = model_id
        self._vectors = {unicodedata.normalize("NFC", k): v for k, v in vectors.items()}
        self._fallback = axis_vector(0.0, fallback_axis or dim - 1, dim)
        self.received_queries: list[str] = []

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def _vector(self, text: str) -> list[float]:
        return list(self._vectors.get(unicodedata.normalize("NFC", text), self._fallback))

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """A scripted query vector, or the query axis itself (so unscripted passages have similarity 0)."""
        self.received_queries.append(text)
        scripted = self._vectors.get(unicodedata.normalize("NFC", text))
        return list(scripted) if scripted is not None else query_vector(self.dim)


def fake_embedder() -> FakeEmbedder:
    """The lexical FakeEmbedder under the real e5-small model key, so its rows match the partial HNSW index."""
    return FakeEmbedder(dim=E5_SMALL_DIM, model_id=E5_SMALL_MODEL_KEY)


@dataclass
class ChunkSpec:
    content: str
    heading: str | None = None
    page_start: int | None = 1
    page_end: int | None = 1
    vector: list[float] | None = None  # overrides the embedder for this chunk
    excluded: bool = False
    flags: tuple[str, ...] = ()
    embed: bool = True  # False: no embedding row at all
    id: uuid.UUID | None = None  # a chosen chunk id (tests that depend on id order)


@dataclass
class SeededDocument:
    document: Document
    version: DocumentVersion
    chunks: list[Chunk] = field(default_factory=list)

    def chunk_id(self, i: int) -> uuid.UUID:
        return self.chunks[i].id


def _script(text: str) -> str:
    deva = sum(1 for ch in text if "ऀ" <= ch <= "ॿ")
    latn = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if deva and not latn:
        return "deva"
    return "latn" if not deva else "mixed"


async def seed_document(
    db: AsyncSession,
    wt: WorldTenant,
    *,
    subject: str,
    classes: tuple[str, ...],
    uploader: User,
    chunks: Sequence[ChunkSpec | str],
    embedder: object | None = None,
    status: str = "published",
    title: str | None = None,
    document: Document | None = None,
    version_no: int = 1,
    model_key: str | None = None,
    original_filename: str = "syllabus.pdf",
    file_kind: str = "pdf",
) -> SeededDocument:
    """Insert a document (or a new version of `document`) with the given chunks and embeddings. Commits."""
    if document is None:
        document = Document(
            tenant_id=wt.id,
            subject_id=wt.subjects[subject].id,
            title=title or f"{subject} notes {uuid.uuid4().hex[:6]}",
            created_by=uploader.id,
        )
        db.add(document)
        await db.flush()
        db.add_all(
            DocumentClass(
                tenant_id=wt.id, document_id=document.id, class_id=wt.classes[c].id, subject_id=document.subject_id
            )
            for c in classes
        )
    specs = [s if isinstance(s, ChunkSpec) else ChunkSpec(content=s) for s in chunks]
    key = model_key or (getattr(embedder, "model_id", None) or E5_SMALL_MODEL_KEY)
    version = DocumentVersion(
        tenant_id=wt.id,
        document_id=document.id,
        version_no=version_no,
        status=status,
        file_kind=file_kind,
        original_filename=original_filename,
        size_bytes=1000,
        sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        storage_key=f"t/{wt.id}/d/{uuid.uuid4().hex}",
        uploaded_by=uploader.id,
        embedding_model=key,
        chunk_count=len(specs),
        flagged_chunk_count=sum(1 for s in specs if s.flags),
        published_at=datetime.now(UTC) if status == "published" else None,
        published_by=uploader.id if status == "published" else None,
    )
    db.add(version)
    await db.flush()

    passages = [f"{s.heading}\n{s.content}" if s.heading else s.content for s in specs]
    computed: list[list[float]] = []
    if embedder is not None:
        computed = embedder.embed_passages(passages)  # type: ignore[attr-defined]
    made: list[Chunk] = []
    for i, spec in enumerate(specs):
        chunk = Chunk(
            **({"id": spec.id} if spec.id is not None else {}),
            tenant_id=wt.id,
            document_version_id=version.id,
            document_id=document.id,
            subject_id=document.subject_id,
            ordinal=i,
            page_start=spec.page_start,
            page_end=spec.page_end,
            heading_path=spec.heading,
            content=spec.content,
            content_sha256=hashlib.sha256(spec.content.encode()).hexdigest(),
            token_count=max(1, len(spec.content.split())),
            script=_script(spec.content),
            injection_flags=list(spec.flags),
            excluded=spec.excluded,
        )
        db.add(chunk)
        made.append(chunk)
    await db.flush()
    for i, (spec, chunk) in enumerate(zip(specs, made, strict=True)):
        vector = spec.vector if spec.vector is not None else (computed[i] if computed else None)
        if spec.embed and vector is not None:
            db.add(
                ChunkEmbedding(
                    tenant_id=wt.id, chunk_id=chunk.id, embedding_model=key, embedding_dim=len(vector), embedding=vector
                )
            )
    await db.commit()
    return SeededDocument(document=document, version=version, chunks=made)


DISTRACTOR_WORD = "quokkafact"
DISTRACTOR_COUNT = 8  # more than the 6 final hits, fewer than the 20 of each ranked side with the test chunks


async def seed_distractors(
    db: AsyncSession, wt: WorldTenant, *, subject: str = "Math", classes: tuple[str, ...] = ("10-A",),
    n: int = DISTRACTOR_COUNT, word: str = DISTRACTOR_WORD,
) -> SeededDocument:
    """A published in-scope document of `n` chunks that all match `word` (put it in the query). They fill the
    final hits, so a leak test whose forbidden chunk ranks below rank 6 still has to catch it in the ranked
    lists (tests/security/retrieval_assertions.py). They are keyword-only (no vector: the FakeEmbedder's
    feature hashing cannot keep a long text's similarity below a real match) and long with one match, so a
    short chunk that really matches the query still ranks above them. The vector side below the final hits
    is covered by prove-it test 8 with scripted vectors."""
    return await seed_document(
        db, wt, subject=subject, classes=classes, uploader=wt.admin,
        chunks=[ChunkSpec(f"{word} distractor {i} {_FILLER}", embed=False) for i in range(n)],
    )


_FILLER = (
    "about ordinary class work with many plain words that carry no meaning for any query in these tests, "
    "only length, so that the ranking of real matches stays above them"
)
