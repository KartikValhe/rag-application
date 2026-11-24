"""
Hit.file_kind (step 6a-1b, F6 of docs/phase6-plan.md): a citation can say "slide 5" or "page 5" because every
retrieval hit carries the file kind of its version. The test-search response does NOT get the field: its
contract (docs/api/phase5.md, TestSearchHit) does not list it and stays as it is.
"""

import re
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth
from tests.fixtures.retrieval_world import ChunkSpec, ScriptedEmbedder, axis_vector, seed_document
from tests.fixtures.search_api import HIT_KEYS, URL, EmbedderSwitch

pytestmark = pytest.mark.asyncio

EMBEDDER = ScriptedEmbedder({})
PHASE5_DOC = Path(__file__).resolve().parents[2] / "docs" / "api" / "phase5.md"


async def _one_of_each_kind(db: AsyncSession, w: WorldTenant, word: str, *, status: str = "published") -> dict[str, uuid.UUID]:
    """A published (or other status) document per file kind in the same class and subject; returns kind -> chunk id."""
    chunk_ids: dict[str, uuid.UUID] = {}
    for kind, vector_axis in (("pdf", 3), ("pptx", 4), ("docx", 5)):
        seeded = await seed_document(
            db, w, subject="Math", classes=("10-A",), uploader=w.t1, status=status, embedder=EMBEDDER,
            file_kind=kind, original_filename=f"lesson.{kind}",
            chunks=[ChunkSpec(f"Lesson {word} in a {kind} file.", page_start=5, page_end=5,
                              vector=axis_vector(0.95, vector_axis))],
        )
        chunk_ids[kind] = seeded.chunk_id(0)
    return chunk_ids


async def test_every_hit_carries_the_file_kind_of_its_version_for_pdf_pptx_and_docx(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    word = f"zq{uuid.uuid4().hex[:10]}"
    chunk_ids = await _one_of_each_kind(raw_db, w, word)

    async with tenant_session_context(str(w.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, w.s1, purpose="read")  # a student enrolled in 10-A
        result = await retrieval.search_for_chat(
            db, scope, word, retrieval.QueryEmbedder.from_embedder(EMBEDDER), retrieval.RetrievalConfig.from_settings()
        )
    kinds = {h.chunk_id: h.file_kind for h in result.hits}
    assert kinds == {chunk_ids["pdf"]: "pdf", chunk_ids["pptx"]: "pptx", chunk_ids["docx"]: "docx"}


async def test_file_kind_is_also_set_on_the_manage_scope_search_with_drafts(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    word = f"zq{uuid.uuid4().hex[:10]}"
    chunk_ids = await _one_of_each_kind(raw_db, w, word, status="ready")

    async with tenant_session_context(str(w.id)) as db:
        scope = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage", include_drafts=True)
        config = retrieval.RetrievalConfig.from_settings()
        embedder = await retrieval.embed_query(retrieval.QueryEmbedder.from_embedder(EMBEDDER), word)
        result = await retrieval.search(db, scope, word, embedder, config, k=10)
    kinds = {h.chunk_id: h.file_kind for h in result.hits}
    assert kinds == {chunk_ids["pdf"]: "pdf", chunk_ids["pptx"]: "pptx", chunk_ids["docx"]: "docx"}


async def test_the_test_search_response_does_not_get_the_field_and_matches_the_documented_contract(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, query_embedder: EmbedderSwitch
) -> None:
    w = academic_world.a
    word = f"zq{uuid.uuid4().hex[:10]}"
    await _one_of_each_kind(raw_db, w, word)
    query_embedder.embedder = EMBEDDER

    resp = await client.post(URL, headers=auth(w, w.t1), json={"query": word})
    assert resp.status_code == 200, resp.text
    hits = resp.json()["hits"]
    assert len(hits) == 3
    documented = _documented_hit_keys()
    assert "file_kind" not in documented  # the contract does not list it: adding it is a contract change
    for hit in hits:
        assert set(hit) == documented == HIT_KEYS


def _documented_hit_keys() -> set[str]:
    """The field names of `TestSearchHit` as docs/api/phase5.md lists them."""
    line = next(ln for ln in PHASE5_DOC.read_text(encoding="utf-8").splitlines() if ln.startswith("- `TestSearchHit`"))
    body = re.search(r"`\{(.*?)\}`", line)
    assert body is not None
    return {part.split(":")[0].strip() for part in body.group(1).split(", ")}
