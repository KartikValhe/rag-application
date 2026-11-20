"""
tests/test_retrieval_quality.py
───────────────────────────────
Synthetic retrieval quality (step 5d-1): two tenants, several classes and subjects, English and Marathi.

  * the right chunk ranks first (FakeEmbedder: lexical vectors; distractors in other pairs and tenant B);
  * RRF beats either side alone (crafted with the ScriptedEmbedder: the vector side's first chunk and the
    keyword side's first chunk are both wrong, the right chunk is second on both);
  * a rare code (ERR_X9 style) is found by the keyword side while its vector is orthogonal to the query;
  * a paraphrase with no shared word is found by the vector side;
  * the HNSW path (candidate cap lowered to 0) returns the same top hits as the exact path.

These are not a quality benchmark (the stand-in embedders are not the real model); the golden-set harness
of step 5d-3 measures the real thing.
"""

import dataclasses
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import retrieval
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.retrieval_world import (
    ChunkSpec,
    ScriptedEmbedder,
    axis_vector,
    fake_embedder,
    seed_document,
)
from tests.security.retrieval_assertions import run_search, search_config

ENGLISH_CHUNKS = [
    "Osmosis is the movement of water across a semi permeable membrane from low to high solute concentration.",
    "A prime number has exactly two divisors: one and itself. Two is the only even prime.",
    "The French Revolution began in 1789 with the storming of the Bastille.",
    "Newton's second law states that force equals mass times acceleration.",
    "A haiku is a short poem of three lines with five, seven and five syllables.",
    "Fractions with equal denominators are added by adding their numerators.",
]
MARATHI_CHUNKS = [
    "प्रकाशसंश्लेषण ही क्रिया पानांमध्ये होते आणि त्यासाठी सूर्यप्रकाश लागतो.",
    "शिवाजी महाराजांनी स्वराज्याची स्थापना केली.",
    "पाण्याचा उत्कलनांक शंभर अंश सेल्सिअस असतो.",
    "त्रिकोणाच्या तीन कोनांची बेरीज एकशे ऐंशी अंश असते.",
]


async def test_the_right_chunk_ranks_first_in_english_and_marathi(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    embedder = fake_embedder()
    mine = await seed_document(raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, embedder=embedder,
                               chunks=[*ENGLISH_CHUNKS, *MARATHI_CHUNKS])
    # Distractors with the SAME text in another pair of tenant A and in tenant B
    await seed_document(raw_db, a, subject="Physics", classes=("10-B",), uploader=a.t2, embedder=embedder,
                        chunks=[*ENGLISH_CHUNKS, *MARATHI_CHUNKS])
    await seed_document(raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, embedder=embedder,
                        chunks=[*ENGLISH_CHUNKS, *MARATHI_CHUNKS])
    cases = [
        ("What is osmosis across a membrane?", 0),
        ("Which number is the only even prime?", 1),
        ("When did the French Revolution begin?", 2),
        ("force equals mass times acceleration", 3),
        ("प्रकाशसंश्लेषण क्रिया कुठे होते?", 6),
        ("पाण्याचा उत्कलनांक किती असतो?", 8),
        ("त्रिकोणाच्या कोनांची बेरीज किती?", 9),
    ]
    for query, index in cases:
        result = await run_search(a, a.t1, query, embedder)
        assert result.hits[0].chunk_id == mine.chunk_id(index), (query, [h.content[:30] for h in result.hits])
        assert {h.document_id for h in result.hits} == {mine.document.id}, "only the caller's pair"


async def test_rrf_beats_either_side_alone(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    query = "osmosis membrane"
    target = "Water moves across a membrane by osmosis in the cells of plants and animals over time."
    vec_only = "Living cells exchange water with their surroundings all the time."
    kw_only = "Osmosis membrane osmosis membrane: quiz words."
    fillers = ["Cells divide by mitosis.", "Leaves contain chlorophyll."]
    embedder = ScriptedEmbedder({})
    doc = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            ChunkSpec(target, vector=axis_vector(0.90, 1)),
            ChunkSpec(vec_only, vector=axis_vector(0.95, 2)),
            ChunkSpec(kw_only, vector=axis_vector(0.10, 3)),
            ChunkSpec(fillers[0], vector=axis_vector(0.50, 4)),
            ChunkSpec(fillers[1], vector=axis_vector(0.40, 5)),
        ],
    )
    result = await run_search(w, w.t1, query, embedder)
    target_id, vec_id, kw_id = doc.chunk_id(0), doc.chunk_id(1), doc.chunk_id(2)
    assert result.vector_ranked[0] == vec_id, "vector side alone ranks the wrong chunk first"
    assert result.keyword_ranked[0] == kw_id, "keyword side alone ranks the wrong chunk first"
    assert result.hits[0].chunk_id == target_id, "fusion ranks the chunk that is second on both sides first"
    top = result.hits[0]
    assert (top.vector_rank, top.keyword_rank) == (2, 2)
    assert top.vector_similarity is not None and abs(top.vector_similarity - 0.90) < 1e-4
    assert top.fused_score == pytest.approx(1 / 62 + 1 / 62)


async def test_a_rare_code_is_found_by_the_keyword_side(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    doc = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            ChunkSpec("Lab manual: the sensor shows ERR_X9 when the probe is dry.", vector=axis_vector(0.0, 1)),
            ChunkSpec("Sensors measure temperature and humidity.", vector=axis_vector(0.80, 2)),
            ChunkSpec("Probes must be cleaned after use.", vector=axis_vector(0.70, 3)),
        ],
    )
    result = await run_search(w, w.t1, "ERR_X9", embedder)
    top = result.hits[0]
    assert top.chunk_id == doc.chunk_id(0)
    assert top.keyword_rank == 1 and top.vector_rank == 3


async def test_a_marathi_paraphrase_is_found_by_the_vector_side(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    query = "झाडे अन्न कसे बनवतात?"  # "how do trees make food?": no word in common with the chunk
    embedder = ScriptedEmbedder({})  # the query gets the query axis; each chunk gets its chosen similarity
    doc = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            ChunkSpec(MARATHI_CHUNKS[1], vector=axis_vector(0.30, 1)),
            ChunkSpec(MARATHI_CHUNKS[0], vector=axis_vector(0.93, 2)),
            ChunkSpec(MARATHI_CHUNKS[2], vector=axis_vector(0.20, 3)),
        ],
    )
    result = await run_search(w, w.t1, query, embedder)
    assert result.keyword_ranked == ()
    top = result.hits[0]
    assert top.chunk_id == doc.chunk_id(1) and top.vector_rank == 1 and top.keyword_rank is None
    assert result.sufficient and result.reason == "similarity"


async def test_below_the_threshold_the_result_is_insufficient(academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
                        chunks=[ChunkSpec("Unrelated text.", vector=axis_vector(0.83, 1))])
    result = await run_search(w, w.t1, "anything at all", embedder)
    assert result.sufficient is False and result.reason == "below_threshold"
    assert result.message == retrieval.INSUFFICIENT_MESSAGE
    assert result.max_similarity is not None and abs(result.max_similarity - 0.83) < 1e-4
    # 5d-5: the default is 0.84, calibrated for the e5-small key the scripted embedder carries
    assert result.threshold == 0.84 and result.threshold_calibrated is True
    assert result.hits, "the hits are still shown to the teacher with their scores"


async def test_the_top_vector_chunk_is_kept_and_sufficiency_comes_from_the_returned_hits(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """A is the best vector match (0.95) but shares no word with the query; B and C are on both sides. With
    k = 2 fusion alone keeps B and C and would drop A, while max_similarity (0.95 from A) said "sufficient":
    the decision would rest on a chunk nobody sees. A must replace the lowest fused hit (C), and every
    number of the decision must come from the returned hits."""
    w = academic_world.a
    embedder = ScriptedEmbedder({})
    doc = await seed_document(
        raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
        chunks=[
            ChunkSpec("Living cells exchange water with their surroundings.", vector=axis_vector(0.95, 1)),
            ChunkSpec("Osmosis moves water across a membrane.", vector=axis_vector(0.80, 2)),
            ChunkSpec("A membrane controls what enters the cell by osmosis.", vector=axis_vector(0.70, 3)),
        ],
    )
    a, b, c = doc.chunk_id(0), doc.chunk_id(1), doc.chunk_id(2)
    result = await run_search(w, w.t1, "osmosis membrane", embedder, k=2)
    assert result.vector_ranked == (a, b, c) and set(result.keyword_ranked) == {b, c}
    fused = retrieval.rrf_fuse(result.vector_ranked, result.keyword_ranked, 60)
    assert a not in {f.id for f in fused[:2]}, "precondition: fusion alone drops the top vector chunk"
    assert [h.chunk_id for h in result.hits] == [fused[0].id, a]
    sims = [h.vector_similarity for h in result.hits if h.vector_similarity is not None]
    assert result.max_similarity is not None and result.max_similarity == max(sims)
    assert abs(result.max_similarity - 0.95) < 1e-4
    matched = [h.keyword_matched_terms for h in result.hits if h.keyword_matched_terms is not None]
    assert result.keyword_coverage == max(matched) / result.query_lexeme_count
    assert result.sufficient and result.reason == "similarity"


async def test_no_candidates_in_scope(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    result = await run_search(w, w.t1, "anything", fake_embedder())
    assert (result.sufficient, result.reason, result.hits, result.candidate_count) == (False, "no_candidates", (), 0)


async def test_the_hnsw_path_returns_the_same_top_hits_as_the_exact_path(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    embedder = fake_embedder()
    marker = uuid.uuid4().hex[:8]
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, embedder=embedder,
                        chunks=[f"{c} ({marker})" for c in ENGLISH_CHUNKS])
    exact_cfg = search_config()
    hnsw_cfg = dataclasses.replace(exact_cfg, exact_max_candidates=0)
    query = "What is osmosis across a membrane?"
    exact = await run_search(w, w.t1, query, embedder, config=exact_cfg)
    hnsw = await run_search(w, w.t1, query, embedder, config=hnsw_cfg)
    assert (exact.path, hnsw.path) == ("exact", "hnsw")
    assert [h.chunk_id for h in hnsw.hits[:3]] == [h.chunk_id for h in exact.hits[:3]]
    assert hnsw.vector_ranked[:3] == exact.vector_ranked[:3]
