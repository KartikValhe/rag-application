"""
tests/test_retrieval_devanagari.py
──────────────────────────────────
Devanagari keyword search (step 5d-1, plan section 4).

The full-text column uses the 'simple' configuration, whose parser decides what a letter is from the
database's LC_CTYPE. On the project image (en_US.utf8) it keeps whole Marathi and Hindi words, matras,
virama, anusvara and conjuncts included, and the danda separates words. These tests pin that on every run
(also in CI), so a database with another locale fails loudly here instead of silently returning nothing
for Marathi questions. Every failure message names the database's LC_CTYPE.

Known limit, not a bug of this step: 'simple' does no stemming, so an inflected form (पाने / पानांमध्ये)
does not match on the keyword side; the vector side and the 5d-3 golden-set numbers decide whether a
trigram index is worth a migration.
"""

from sqlalchemy import text

from app.core.tenant_db import tenant_session_context
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.retrieval_world import ChunkSpec, ScriptedEmbedder, seed_document
from tests.security.retrieval_assertions import run_search

MARATHI = "महाराष्ट्राची राजधानी मुंबई आहे. प्रकाशसंश्लेषण क्रिया पानांमध्ये होते."
MARATHI_LEXEMES = {"महाराष्ट्राची", "राजधानी", "मुंबई", "आहे", "प्रकाशसंश्लेषण", "क्रिया", "पानांमध्ये", "होते"}
HINDI = "भारत का संविधान 26 जनवरी 1950 को लागू हुआ।"
HINDI_LEXEMES = {"भारत", "का", "संविधान", "26", "जनवरी", "1950", "को", "लागू", "हुआ"}


async def _ctype_and_lexemes(tenant_id: str, value: str) -> tuple[str, set[str]]:
    async with tenant_session_context(tenant_id) as db:
        ctype = (
            await db.execute(text("SELECT datctype FROM pg_database WHERE datname = current_database()"))
        ).scalar_one()
        lexemes = (
            await db.execute(text("SELECT tsvector_to_array(to_tsvector('simple', :v))"), {"v": value})
        ).scalar_one()
    return str(ctype), set(lexemes)


async def test_marathi_words_stay_whole(academic_world: AcademicWorld) -> None:
    ctype, lexemes = await _ctype_and_lexemes(str(academic_world.a.id), MARATHI)
    assert lexemes == MARATHI_LEXEMES, f"LC_CTYPE={ctype}: the 'simple' parser split Marathi words: {sorted(lexemes)}"


async def test_hindi_words_stay_whole_and_the_danda_separates(academic_world: AcademicWorld) -> None:
    ctype, lexemes = await _ctype_and_lexemes(str(academic_world.a.id), HINDI)
    assert lexemes == HINDI_LEXEMES, f"LC_CTYPE={ctype}: {sorted(lexemes)}"
    ctype, glued = await _ctype_and_lexemes(str(academic_world.a.id), "मुंबई आहे।पुणे शहर॥गाव")
    assert glued == {"मुंबई", "आहे", "पुणे", "शहर", "गाव"}, f"LC_CTYPE={ctype}: danda did not split: {sorted(glued)}"


async def test_a_marathi_question_finds_its_chunk_through_the_keyword_side(
    academic_world: AcademicWorld, raw_db: object
) -> None:
    """The natural question has words the chunk lacks (म्हणजे, काय): the OR query still matches, and the hit
    comes from the KEYWORD side (the scripted embedder gives every chunk similarity 0)."""
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    doc = await seed_document(
        raw_db,  # type: ignore[arg-type]
        w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            ChunkSpec(MARATHI, heading="विज्ञान > वनस्पती"),
            ChunkSpec("गुणाकार आणि भागाकार यांचे नियम."),
            ChunkSpec("त्रिकोणाच्या कोनांची बेरीज १८० अंश असते."),
        ],
    )
    result = await run_search(w, w.t1, "प्रकाशसंश्लेषण म्हणजे काय?", embedder)
    assert result.hits, "no hit for a Marathi question whose key word is in the chunk"
    top = result.hits[0]
    assert top.chunk_id == doc.chunk_id(0)
    assert top.keyword_rank == 1 and top.keyword_matched_terms == 1
    assert result.query_lexeme_count == 3  # प्रकाशसंश्लेषण, म्हणजे, काय


async def test_a_hindi_question_finds_its_chunk_through_the_keyword_side(
    academic_world: AcademicWorld, raw_db: object
) -> None:
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    doc = await seed_document(
        raw_db,  # type: ignore[arg-type]
        w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[ChunkSpec("गुणा और भाग के नियम।"), ChunkSpec(HINDI)],
    )
    result = await run_search(w, w.t1, "संविधान कब लागू हुआ?", embedder)
    assert result.hits and result.hits[0].chunk_id == doc.chunk_id(1)
    assert result.hits[0].keyword_matched_terms == 3  # संविधान, लागू, हुआ


async def test_inflected_marathi_forms_do_not_match_on_the_keyword_side(
    academic_world: AcademicWorld, raw_db: object
) -> None:
    """Characterisation of the known limit (plan F4): पाने does not match पानांमध्ये. If this starts to
    pass, a stemmer or trigram index was added: update plan section 4 and this test."""
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    await seed_document(
        raw_db,  # type: ignore[arg-type]
        w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder, chunks=[ChunkSpec(MARATHI)],
    )
    result = await run_search(w, w.t1, "पाने", embedder)
    assert all(h.keyword_rank is None for h in result.hits)
