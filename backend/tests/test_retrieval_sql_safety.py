"""
tests/test_retrieval_sql_safety.py
──────────────────────────────────
User text never becomes SQL (step 5d-1). Hostile queries run without an error, and a recorder on the
session shows that neither the query nor any lexeme of it appears in a SQL statement: they travel only
as bind parameters. The OR tsquery text round-trips through Postgres for awkward lexemes.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.retrieval_world import fake_embedder, seed_document
from tests.security.retrieval_assertions import search_config

HOSTILE = [
    "'; DROP TABLE app.chunks; --",
    "x' OR '1'='1",
    "a\\b \\' \\\\ '' \"",
    "fractions & | ! ( ) : * <-> <2> :* && ||",
    "see http://ex.com/a'b\\c?x=\"y\" and C:\\temp\\x and o'neil",
    "$$ $q$ E'\\x41' /* */ -- ;",
    "प्रकाश" * 80,  # 480 Devanagari characters in one word
    "{schema} {dim} %s %(t)s :t :qv",
]


async def _recorded_search(w: Any, query: str) -> tuple[retrieval.RetrievalResult, list[str]]:
    """Search as T1 and record the text of every SQL statement the service executes."""
    statements: list[str] = []
    async with tenant_session_context(str(w.id)) as db:
        real_execute = db.execute

        async def recording_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(str(statement))
            return await real_execute(statement, *args, **kwargs)

        scope = await retrieval.build_retrieval_scope(db, w.t1, purpose="manage")
        db.execute = recording_execute  # type: ignore[method-assign]
        result = await retrieval.search(
            db, scope, query, retrieval.QueryEmbedder.from_embedder(fake_embedder()),
            search_config(),
        )
    return result, statements


@pytest.mark.parametrize("query", HOSTILE)
async def test_hostile_queries_run_and_never_change_the_sql_text(
    query: str, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """The SQL text is the same for every query: user text only ever travels as bind parameters."""
    w = academic_world.a
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=fake_embedder(),
                        chunks=[f"Fractions and o'neil notes zq{uuid.uuid4().hex[:8]}."])
    _, baseline = await _recorded_search(w, "fractions")
    result, statements = await _recorded_search(w, query)
    assert result.candidate_count == 1
    assert statements == baseline, "the SQL text changed with the query"
    assert all(query not in s for s in statements)


async def test_or_tsquery_round_trips_awkward_lexemes(academic_world: AcademicWorld) -> None:
    """Each lexeme Postgres produces, quoted by or_tsquery_text, matches exactly itself."""
    sample = "see http://ex.com/a'b\\c?x=\"y\" it's x-ray ERR_X9 /usr/bin/a'b प्रकाशसंश्लेषण"
    async with tenant_session_context(str(academic_world.a.id)) as db:
        lexemes = (await db.execute(text("SELECT tsvector_to_array(to_tsvector('simple', :q))"), {"q": sample})).scalar_one()
        assert any("'" in lx for lx in lexemes), "the sample must produce a lexeme with a quote"
        for lexeme in lexemes:
            tsq = retrieval.or_tsquery_text([lexeme])
            ok = (
                await db.execute(
                    text("SELECT array_to_tsvector(CAST(:a AS text[])) @@ CAST(CAST(:q AS text) AS tsquery), "
                         "array_to_tsvector(CAST(:other AS text[])) @@ CAST(CAST(:q AS text) AS tsquery)"),
                    {"a": [lexeme], "other": [lexeme + "zz"], "q": tsq},
                )
            ).one()
            assert tuple(ok) == (True, False), f"{lexeme!r} -> {tsq!r}"
