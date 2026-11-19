"""
tests/test_eval_retrieval_db.py
───────────────────────────────
The database side of the retrieval evaluation harness (step 5d-3), on SYNTHETIC documents only (the test
database, the academic_world and retrieval_world fixtures, a scripted embedder). The real golden set and the
real model are never used here, and the harness is never run against the dev database by a test.

  * the whole command runs end to end and its report carries the right numbers (hit@k, MRR, answer rate,
    refusal accuracy, per-language split, keyword-only / vector-only hit@k, the sweep and its two thresholds,
    the corpus: chunks per document and flagged chunks);
  * it reads published versions of ONE pair of ONE tenant only (another tenant, another pair, a ready version
    and an excluded chunk never count);
  * the search runs in a READ ONLY transaction (a write is refused), the tenant lookup too, and the data is
    unchanged afterwards;
  * missing documents give "skipped" and exit 0 before the model is loaded; an unknown tenant or a pair the
    tenant does not offer is an error;
  * it prints ids, titles, pages and headings only: never chunk text.
"""

import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.document import Chunk, Document, DocumentVersion
from app.ml.model_keys import E5_SMALL_DIM
from app.scripts import eval_retrieval
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.retrieval_world import ChunkSpec, ScriptedEmbedder, seed_document

HEADER = "#,Language,Question,Document,Expected location,Should answer\n"

# Chunk texts. None of them may appear in the command's output or its report.
OSMOSIS = "Osmosis is the movement of water across a membrane."
PRIMES = "A prime number has exactly two divisors."
MARATHI = "प्रकाशसंश्लेषणाची क्रिया पानांमध्ये होते."
WELCOME = "Course welcome and rules of the class."
TIMETABLE = "Lessons start at nine each morning."
EXCLUDED = "Hidden slide that must not be counted."
DRAFT = "Draft chapter that is only ready, not published."
OTHER_PAIR = "Physics lesson for another class."
CHUNK_TEXTS = (OSMOSIS, PRIMES, MARATHI, WELCOME, TIMETABLE, EXCLUDED, DRAFT, OTHER_PAIR)


def unit(components: dict[int, float]) -> list[float]:
    vec = [0.0] * E5_SMALL_DIM
    for axis, value in components.items():
        vec[axis] = value
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


def query_with(axis: int, sim: float) -> list[float]:
    """A query vector whose cosine to the chunk vector `unit({axis: 1})` is `sim` (the rest lies on axis 9)."""
    return unit({axis: sim, 9: math.sqrt(1 - sim * sim)})


# (number, language, question, document, expected location, should answer, query vector)
# Similarities avoid the 0.01 grid (pgvector stores float4), so no threshold sits on a value.
QUESTIONS: list[tuple[str, str, str, str, str, str, list[float]]] = [
    ("1", "English", "osmosis membrane", "Biology Notes", "page 1", "Yes", query_with(0, 0.955)),
    ("2", "English", "divisors prime", "Biology Notes", "page 3", "Yes", query_with(1, 0.905)),
    ("3", "Marathi", "प्रकाशसंश्लेषण", "Biology Notes", "heading: प्रकाश", "Yes", query_with(2, 0.805)),
    ("4", "English", "welcome rules", "Deck", "slide 9", "Yes", query_with(3, 0.915)),
    ("5", "English", "zebra giraffes", "(none)", "", "No", query_with(4, 0.405)),
    ("6", "English", "osmosis tide", "(none)", "", "No", query_with(0, 0.895)),
    ("7", "Marathi", "बुद्धिबळ खेळ", "(none)", "", "No", query_with(3, 0.205)),
    ("8", "Marathi", "पानांमध्ये क्रिया", "Biology Notes", "heading: प्रकाश", "Yes", query_with(2, 0.885)),
]


def golden_text() -> str:
    lines = [",".join(f'"{cell}"' for cell in q[:6]) for q in QUESTIONS]
    return "\ufeff" + HEADER + "\n".join(lines) + "\n"


def scripted_embedder() -> ScriptedEmbedder:
    return ScriptedEmbedder({q[2]: q[6] for q in QUESTIONS})


async def seed_world(raw_db: AsyncSession, academic_world: AcademicWorld) -> tuple[WorldTenant, Any, Any]:
    """Tenant A pair (10-A, Math): two published documents, an excluded chunk, a flagged chunk, a ready document.
    Elsewhere: another pair of the same tenant, and tenant B with the same titles."""
    a, b = academic_world.a, academic_world.b
    biology = await seed_document(
        raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, title="Biology Notes",
        original_filename="bio.pdf",
        chunks=[
            ChunkSpec(OSMOSIS, heading="Unit 1 > Osmosis", page_start=1, page_end=1, vector=unit({0: 1})),
            ChunkSpec(PRIMES, heading="Unit 2 > Primes", page_start=2, page_end=3, vector=unit({1: 1}),
                      flags=("instruction_override",)),
            ChunkSpec(MARATHI, heading="घटक ३ > प्रकाशसंश्लेषण", page_start=4, page_end=4, vector=unit({2: 1})),
        ],
    )
    deck = await seed_document(
        raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, title="Deck", original_filename="deck.pptx",
        chunks=[
            ChunkSpec(WELCOME, heading="Welcome", page_start=1, page_end=1, vector=unit({3: 1})),
            ChunkSpec(TIMETABLE, heading="Timetable", page_start=2, page_end=2, vector=unit({4: 1})),
            ChunkSpec(EXCLUDED, heading="Hidden", page_start=3, page_end=3, vector=unit({3: 1}), excluded=True),
        ],
    )
    await seed_document(  # ready, not published: never part of the evaluation
        raw_db, a, subject="Math", classes=("10-A",), uploader=a.t1, title="Draft Notes", status="ready",
        chunks=[ChunkSpec(DRAFT, vector=unit({0: 1}))],
    )
    await seed_document(  # another pair of the same tenant
        raw_db, a, subject="Physics", classes=("10-B",), uploader=a.t2, title="Physics Notes",
        chunks=[ChunkSpec(OTHER_PAIR, vector=unit({0: 1}))],
    )
    await seed_document(  # tenant B, the same title and the same text
        raw_db, b, subject="Math", classes=("10-A",), uploader=b.t1, title="Biology Notes", original_filename="bio.pdf",
        chunks=[ChunkSpec(OSMOSIS, heading="Unit 1 > Osmosis", page_start=1, page_end=1, vector=unit({0: 1}))],
    )
    return a, biology, deck


def args(w: WorldTenant, tmp_path: Path, *extra: str, golden: Path | None = None, pair: tuple[Any, Any] | None = None,
         tenant: str | None = None) -> list[str]:
    class_id, subject_id = pair or w.pair("10-A", "Math")
    return [
        "--tenant", tenant or w.tenant.slug, "--class-id", str(class_id), "--subject-id", str(subject_id),
        "--golden", str(golden or write_golden(tmp_path)), "--report", str(tmp_path / "report.json"), *extra,
    ]


def write_golden(tmp_path: Path) -> Path:
    path = tmp_path / "golden.csv"
    path.write_text(golden_text(), encoding="utf-8")
    return path


def never_load() -> Any:
    raise AssertionError("the model must not be loaded in this case")


@pytest.fixture(autouse=True)
def _threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SIMILARITY", 0.85)
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_KEYWORD_COVERAGE", 0.0)


async def counts(db: AsyncSession) -> tuple[int, int, int, Any]:
    await db.commit()
    return (
        (await db.execute(select(func.count()).select_from(Chunk))).scalar_one(),
        (await db.execute(select(func.count()).select_from(DocumentVersion))).scalar_one(),
        (await db.execute(select(func.count()).select_from(Document))).scalar_one(),
        (await db.execute(select(func.max(DocumentVersion.updated_at)))).scalar_one(),
    )


# ── the whole command, end to end ────────────────────────────────────────────


async def test_the_command_runs_end_to_end_and_the_report_has_the_right_numbers(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    before = await counts(raw_db)

    seen: list[str] = []
    real = eval_retrieval.readonly_tenant_session

    @asynccontextmanager
    async def spy(tenant_id: Any) -> AsyncIterator[AsyncSession]:
        async with real(tenant_id) as db:
            seen.append((await db.execute(text("SHOW transaction_read_only"))).scalar_one())
            yield db

    monkeypatch.setattr(eval_retrieval, "readonly_tenant_session", spy)
    code = await eval_retrieval.run(args(w, tmp_path), embedder_loader=scripted_embedder)
    out = capsys.readouterr().out
    assert code == 0, out
    assert seen == ["on"], "the search session is a READ ONLY transaction"

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    meta = report["meta"]
    assert meta["tenant"] == w.tenant.slug and meta["k"] == 10
    assert meta["threshold"] == 0.85 and meta["threshold_calibrated"] is False and meta["model"]
    assert meta["keyword_max_candidates"] == 2000 and "cannot drop a better match" in out, "B97: 5 chunks, cap 2000"

    # should-answer rows: 1 2 3 4 8; should-not-answer rows: 5 6 7
    overall = report["overall"]
    assert (overall["should_answer"], overall["should_not_answer"]) == (5, 3)
    assert overall["hit_at"] == {"1": 4 / 5, "3": 4 / 5, "5": 4 / 5, "10": 4 / 5}
    assert overall["mrr"] == pytest.approx(4 / 5)
    assert overall["answer_rate"] == pytest.approx(4 / 5)  # question 3 is at 0.805, below 0.85
    assert overall["refusal_accuracy"] == pytest.approx(2 / 3)  # question 6 is answered at 0.895
    assert overall["keyword_only_hit_at"]["1"] == 3 / 5  # 1, 2 and 8; question 3 is an inflected Marathi word
    assert overall["vector_only_hit_at"]["1"] == 4 / 5
    assert overall["outcomes"] == {
        "ok": 3, "below_threshold": 1, "wrong_chunk": 1, "no_candidate": 0, "document_not_in_scope": 0,
        "refused_correctly": 2, "answered_anyway": 1,
    }

    english, marathi = report["by_language"]["English"], report["by_language"]["Marathi"]
    assert (english["should_answer"], english["should_not_answer"]) == (3, 2)
    assert english["hit_at"]["1"] == pytest.approx(2 / 3) and english["answer_rate"] == 1.0
    assert english["refusal_accuracy"] == 0.5
    assert (marathi["should_answer"], marathi["should_not_answer"]) == (2, 1)
    assert marathi["hit_at"]["1"] == 1.0 and marathi["answer_rate"] == 0.5 and marathi["refusal_accuracy"] == 1.0
    assert marathi["keyword_only_hit_at"]["1"] == 0.5 and marathi["vector_only_hit_at"]["1"] == 1.0

    by_number = {q["number"]: q for q in report["questions"]}
    assert [q["outcome"] for q in report["questions"]] == [
        "ok", "ok", "below_threshold", "wrong_chunk", "refused_correctly", "answered_anyway", "refused_correctly", "ok",
    ]
    q1 = by_number["1"]
    assert q1["hit_position"] == 1 and q1["sufficient"] is True and q1["max_similarity"] == pytest.approx(0.955, abs=1e-4)
    assert q1["top"][0]["document_title"] == "Biology Notes" and q1["top"][0]["heading_path"] == "Unit 1 > Osmosis"
    assert q1["top"][0]["vector_similarity"] == pytest.approx(0.955, abs=1e-4)
    assert q1["top"][0]["keyword_matched_terms"] == 2 and q1["top"][0]["is_match"] is True
    assert len(q1["top"]) == 5
    assert by_number["4"]["hit_position"] is None and by_number["3"]["max_similarity"] == pytest.approx(0.805, abs=1e-4)

    sweep = report["sweep"]
    assert [r["threshold"] for r in sweep["rows"]][:2] == [0.70, 0.71] and len(sweep["rows"]) == 26
    at = {r["threshold"]: r for r in sweep["rows"]}
    assert (at[0.80]["answer_rate"], at[0.80]["refusal_accuracy"]) == (1.0, pytest.approx(2 / 3))
    assert (at[0.81]["answer_rate"], at[0.81]["refusal_accuracy"]) == (pytest.approx(4 / 5), pytest.approx(2 / 3))
    assert (at[0.90]["answer_rate"], at[0.90]["refusal_accuracy"]) == (pytest.approx(3 / 5), 1.0)
    assert sweep["best_balanced"]["threshold"] == 0.80
    assert sweep["highest_keeping_answer_rate"]["threshold"] == 0.80

    corpus = meta["corpus"]
    assert corpus["chunks"] == 5 and corpus["flagged_chunks"] == 1, "excluded chunks and other pairs do not count"
    docs = {d["title"]: d for d in corpus["documents"]}
    assert set(docs) == {"Biology Notes", "Deck"}, "no ready version, no other pair, no other tenant"
    assert (docs["Biology Notes"]["chunks"], docs["Biology Notes"]["flagged"]) == (3, 1)
    assert (docs["Deck"]["chunks"], docs["Deck"]["flagged"]) == (2, 0)

    # ids, titles, pages and headings only: never chunk text
    written = (tmp_path / "report.json").read_text(encoding="utf-8")
    for body in CHUNK_TEXTS:
        assert body not in out and body not in written
    for phrase in ("Biology Notes", "hit@1", "answer rate", "refusal accuracy", "best balanced threshold",
                   "found but below the threshold", "wrong chunk", "not calibrated"):
        assert phrase in out, phrase

    assert await counts(raw_db) == before, "the harness changed nothing"

    # the same input gives the same numbers
    first = json.loads(written)
    first["meta"].pop("generated_at", None)
    assert await eval_retrieval.run(args(w, tmp_path), embedder_loader=scripted_embedder) == 0
    second = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    second["meta"].pop("generated_at", None)
    assert second == first


async def test_a_smaller_k_limits_the_hits_and_the_columns(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path
) -> None:
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path, "--k", "2"), embedder_loader=scripted_embedder) == 0
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["meta"]["k"] == 2
    assert all(len(q["top"]) <= 2 for q in report["questions"])
    assert set(report["overall"]["hit_at"]) == {"1", "2", "3", "5"}


async def test_the_configured_threshold_is_read_not_chosen(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `sufficient` follows the setting; the similarities and the sweep do not."""
    w, _, _ = await seed_world(raw_db, academic_world)
    assert await eval_retrieval.run(args(w, tmp_path), embedder_loader=scripted_embedder) == 0
    default = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SIMILARITY", 0.80)
    assert await eval_retrieval.run(args(w, tmp_path), embedder_loader=scripted_embedder) == 0
    lower = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert lower["meta"]["threshold"] == 0.80 and lower["overall"]["answer_rate"] == 1.0
    assert lower["sweep"]["rows"] == default["sweep"]["rows"]
    assert [q["max_similarity"] for q in lower["questions"]] == [q["max_similarity"] for q in default["questions"]]


# ── READ ONLY ────────────────────────────────────────────────────────────────


async def test_the_tenant_session_is_read_only_and_a_write_is_refused(
    academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w, biology, _ = await seed_world(raw_db, academic_world)
    before = await counts(raw_db)
    async with eval_retrieval.readonly_tenant_session(w.id) as db:
        assert (await db.execute(text("SHOW transaction_read_only"))).scalar_one() == "on"
        assert (await db.execute(select(func.count()).select_from(Chunk))).scalar_one() > 0, "reads work, with RLS"
        assert (await db.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one() == str(w.id)
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await db.execute(text("UPDATE app.chunks SET excluded = true WHERE id = :id"), {"id": biology.chunk_id(0)})
    assert await counts(raw_db) == before
    excluded = (await raw_db.execute(select(Chunk.excluded).where(Chunk.id == biology.chunk_id(0)))).scalar_one()
    assert excluded is False


async def test_the_tenant_lookup_is_read_only_too(academic_world: AcademicWorld) -> None:
    w = academic_world.a
    async with eval_retrieval.readonly_platform_session() as db:
        assert (await db.execute(text("SHOW transaction_read_only"))).scalar_one() == "on"
        with pytest.raises(DBAPIError):
            await db.execute(text("UPDATE app.tenants SET name = name WHERE id = :id"), {"id": w.id})
    assert await eval_retrieval.resolve_tenant(w.tenant.slug) == w.id
    assert await eval_retrieval.resolve_tenant("no-such-tenant") is None


# ── skips and errors ─────────────────────────────────────────────────────────


async def test_no_published_documents_is_a_skip_before_the_model_is_loaded(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w = academic_world.a
    out_empty = await eval_retrieval.run(args(w, tmp_path), embedder_loader=never_load)
    text_out = capsys.readouterr().out
    assert out_empty == 0 and text_out.strip().startswith("skipped:") and "published documents" in text_out
    # a version that is only ready is not published
    await seed_document(raw_db, w, subject="Math", classes=("10-A",), uploader=w.t1, status="ready", chunks=[OSMOSIS],
                        title="Draft")
    assert await eval_retrieval.run(args(w, tmp_path), embedder_loader=never_load) == 0
    assert "skipped:" in capsys.readouterr().out
    assert not (tmp_path / "report.json").exists()


async def test_an_unknown_tenant_and_a_pair_the_tenant_does_not_offer_are_errors(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    w, other = academic_world.a, academic_world.b
    assert await eval_retrieval.run(args(w, tmp_path, tenant="no-such-tenant"), embedder_loader=never_load) == 2
    assert "unknown tenant" in capsys.readouterr().err
    # a pair of tenant B is not offered by tenant A
    code = await eval_retrieval.run(args(w, tmp_path, pair=other.pair("10-A", "Math")), embedder_loader=never_load)
    assert code == 2 and "not offered" in capsys.readouterr().err
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize(("question", "code"), [("???", "invalid_query"), ("word " * 120, "query_too_long")])
async def test_a_golden_question_that_cannot_be_searched_is_an_error_naming_the_row(
    academic_world: AcademicWorld, raw_db: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    question: str, code: str,
) -> None:
    """Found before the model is loaded: a golden-file mistake, not a failed question."""
    w, _, _ = await seed_world(raw_db, academic_world)
    path = tmp_path / "golden.csv"
    rows = '1,English,"Fine one",Biology Notes,page 1,Yes\n' + f'2,English,"{question}",Biology Notes,page 1,Yes\n'
    path.write_text(HEADER + rows, encoding="utf-8")
    assert await eval_retrieval.run(args(w, tmp_path, golden=path), embedder_loader=never_load) == 2
    err = capsys.readouterr().err
    assert "row 3" in err and code in err
    assert not (tmp_path / "report.json").exists()
