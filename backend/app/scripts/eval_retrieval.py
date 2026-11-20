"""
app/scripts/eval_retrieval.py
─────────────────────────────
Operator command (step 5d-3, docs/phase5d-plan.md section 10): measure how well retrieval finds the right
chunk for a golden set of questions, on the REAL documents of ONE tenant's class and subject, with the REAL
embedding model. Retrieval only: there is no LLM call anywhere.

    python -m app.scripts.eval_retrieval --tenant acme --class-id <uuid> --subject-id <uuid> \
        [--golden tests/fixtures/golden_set.csv] [--k 10] --report <path.json> [--compare [--grid vw=1,2 df=off,0.3]]

With --compare (step 5d-4) the golden set runs once per retrieval configuration (fusion weights, keyword
pruning, minimum matched terms: a grid, the current settings and a vector-only baseline) and ONE table is
printed: hit@1, hit@3, hit@5 and MRR per language, the answer and refusal numbers at the current threshold,
and how many questions changed their hit@1 against the current settings. Every question is embedded once;
every configuration goes through the same retrieval.search (the config is overridden per run, no copy of the
SQL); the run stays READ ONLY and changes no setting. docs/api/phase5.md section 12 says how to read it.

What it does
  1. reads the golden CSV (columns #, Language, Question, Document, Expected location, Should answer);
  2. finds the tenant by slug (a READ ONLY platform transaction), then opens ONE READ ONLY tenant transaction
     as app_tenant (RLS on) and builds the scope with RetrievalScope.for_operator: one offered class and
     subject pair, PUBLISHED versions only;
  3. loads the real embedder from EMBEDDING_MODEL_DIR and runs every question through the same
     retrieval.search the API uses (the settings come from the environment, like the API);
  4. writes a JSON report and prints a plain-English summary: hit@k, MRR, answer rate, refusal accuracy per
     language, the keyword-only and vector-only hit@k (for the trigram decision, backlog B92), the questions
     that failed and why, the threshold sweep, the chunk count per document and the flagged chunk count.

It skips cleanly (prints "skipped: <reason>", exit code 0) when the golden file, the model directory or the
documents are missing. A malformed golden file, an unknown tenant, a pair the tenant does not offer and a
question that cannot be searched are errors (exit code 2, the message names the row).

Safety
  * Nothing is written to the database: both transactions are READ ONLY (a write is refused by Postgres).
  * It prints and reports ids, titles, file names, pages and headings only, never chunk text. Titles and
    headings come from uploaded files, so the console copy is cleaned (retrieval_eval.safe_text).
  * It never sets a threshold. RETRIEVAL_MIN_SIMILARITY is only read and reported; the owner chooses a value
    from the sweep (docs/api/phase5.md, section 12). Since 5d-5 the report says how the threshold was calibrated
    (date, model, golden-set size, from retrieval.CALIBRATION) when the model is the calibrated one, and prints a
    WARNING when it is not.
  * With the keyword side switched off (RETRIEVAL_RRF_KEYWORD_WEIGHT 0, the default since 5d-5) the keyword-only
    numbers are not measured (shown as n/a); `--compare` still runs hybrid rows.
  * Run it against the database you mean: it uses DATABASE_URL and PLATFORM_DATABASE_URL like the API. Tests
    run it only against the test database.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.document import VERSION_STATUS_PUBLISHED
from app.db.models.tenant import Tenant
from app.ml.lock import DEFAULT_LOCK_PATH, ModelLoadError, load_lock
from app.services import retrieval
from app.services import retrieval_eval as ev

DEFAULT_GOLDEN = "tests/fixtures/golden_set.csv"
MAX_K = retrieval.MAX_FINAL_K
EXIT_ERROR = 2

EmbedderLoader = Callable[[], Any]  # blocking: returns an embedder (real or scripted)


class Skipped(Exception):  # noqa: N818 (a control-flow signal, printed as "skipped: <reason>")
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CommandError(Exception):
    """A problem the operator must fix (printed to stderr, exit code 2)."""


# ── READ ONLY sessions ───────────────────────────────────────────────────────


@contextlib.asynccontextmanager
async def readonly_platform_session() -> AsyncIterator[AsyncSession]:
    """A platform-role transaction that cannot write: SET TRANSACTION READ ONLY is its first statement."""
    from app.db.engine import PlatformSessionLocal

    async with PlatformSessionLocal() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session


@contextlib.asynccontextmanager
async def readonly_tenant_session(tenant_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Like tenant_session_context (app_tenant, RLS on, app.tenant_id set for this transaction only), but READ
    ONLY: Postgres refuses any INSERT, UPDATE or DELETE here, whatever the code does."""
    from app.db.engine import SessionLocal

    async with SessionLocal() as session, session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        yield session


async def resolve_tenant(slug: str) -> uuid.UUID | None:
    async with readonly_platform_session() as db:
        found = (await db.execute(select(Tenant.id).where(Tenant.slug == slug))).scalar_one_or_none()
    return found


# ── the model ────────────────────────────────────────────────────────────────


def check_model_available() -> None:
    """Skip (not fail) when the model files are not on this machine. A model that is there but fails its
    verification is an error, reported by the loader."""
    root = settings.EMBEDDING_MODEL_DIR
    if not root:
        raise Skipped(
            "EMBEDDING_MODEL_DIR is not set (the real embedding model is needed: "
            "python -m app.scripts.fetch_model e5-small --out <dir>, then set EMBEDDING_MODEL_DIR)"
        )
    root_path = Path(root)
    if not root_path.is_dir():
        raise Skipped(f"EMBEDDING_MODEL_DIR is not a directory: {root}")
    try:
        spec = load_lock(DEFAULT_LOCK_PATH).get(settings.EMBEDDING_MODEL_ID)
    except ModelLoadError as exc:
        raise CommandError(f"the configured model cannot be used: {exc}") from None
    if not (root_path / spec.directory_name).is_dir():
        raise Skipped(
            f"the model files for {spec.name} are not in {root} (expected a folder named {spec.directory_name}; "
            f"python -m app.scripts.fetch_model {spec.name} --out {root})"
        )


def _load_real_embedder() -> Any:
    from app.ml import loader

    try:
        return loader.get_embedder()
    except ModelLoadError as exc:
        raise CommandError(f"the embedding model failed to load or to verify: {exc}") from None


# ── reading the corpus and one search ────────────────────────────────────────

CORPUS_SQL = """
SELECT d.id AS document_id, d.title, v.id AS version_id, v.version_no, v.original_filename,
       count(c.id) AS chunks,
       count(c.id) FILTER (WHERE cardinality(c.injection_flags) > 0) AS flagged
FROM app.documents d
JOIN app.document_versions v ON v.tenant_id = d.tenant_id AND v.document_id = d.id AND v.status = :published
LEFT JOIN app.chunks c ON c.tenant_id = v.tenant_id AND c.document_version_id = v.id AND NOT c.excluded
WHERE d.tenant_id = :t AND d.deleted_at IS NULL AND d.subject_id = :s
  AND EXISTS (
      SELECT 1 FROM app.document_classes dc
      WHERE dc.tenant_id = d.tenant_id AND dc.document_id = d.id AND dc.class_id = :c AND dc.subject_id = d.subject_id
  )
GROUP BY d.id, d.title, v.id, v.version_no, v.original_filename
ORDER BY d.title, d.id
"""

# Where the chunks of the two single-side rankings are, for the keyword-only and vector-only numbers.
CHUNK_PLACES_SQL = """
SELECT c.id, d.title, v.original_filename, c.page_start, c.page_end, c.heading_path
FROM app.chunks c
JOIN app.documents d ON d.tenant_id = c.tenant_id AND d.id = c.document_id
JOIN app.document_versions v ON v.tenant_id = c.tenant_id AND v.id = c.document_version_id
WHERE c.tenant_id = :t AND c.id = ANY(CAST(:ids AS uuid[]))
"""


async def read_corpus(db: AsyncSession, tenant_id: uuid.UUID, class_id: uuid.UUID, subject_id: uuid.UUID) -> dict[str, Any]:
    """Published documents of the pair, with chunk counts (excluded chunks do not count) and flagged counts."""
    rows = (
        await db.execute(
            text(CORPUS_SQL),
            {"published": VERSION_STATUS_PUBLISHED, "t": tenant_id, "s": subject_id, "c": class_id},
        )
    ).all()
    documents = [
        {
            "document_id": str(r.document_id), "version_id": str(r.version_id), "version_no": r.version_no,
            "title": r.title, "original_filename": r.original_filename, "chunks": int(r.chunks), "flagged": int(r.flagged),
        }
        for r in rows
    ]
    return {
        "documents": documents,
        "chunks": sum(d["chunks"] for d in documents),
        "flagged_chunks": sum(d["flagged"] for d in documents),
    }


def _hit_ref(h: retrieval.Hit) -> ev.ChunkRef:
    return ev.ChunkRef(
        chunk_id=str(h.chunk_id), document_title=h.document_title, original_filename=h.original_filename,
        page_start=h.page_start, page_end=h.page_end, heading_path=h.heading_path,
        vector_similarity=h.vector_similarity, keyword_matched_terms=h.keyword_matched_terms, fused_score=h.fused_score,
    )


async def retrieve_one(
    db: AsyncSession, scope: retrieval.RetrievalScope, query: str, query_embedder: retrieval.QueryEmbedder,
    config: retrieval.RetrievalConfig, k: int,
) -> tuple[ev.Retrieved, retrieval.RetrievalResult]:
    """One question through retrieval.search (the service the API uses), plus the places (never the text) of
    the chunks that only the single-side rankings list."""
    result = await retrieval.search(db, scope, query, query_embedder, config, k=k)
    known = {h.chunk_id: _hit_ref(h) for h in result.hits}
    missing = [i for i in (*result.vector_ranked, *result.keyword_ranked) if i not in known]
    if missing:
        rows = (await db.execute(text(CHUNK_PLACES_SQL), {"t": scope.tenant_id, "ids": list(dict.fromkeys(missing))})).all()
        for r in rows:
            known[r.id] = ev.ChunkRef(
                chunk_id=str(r.id), document_title=r.title, original_filename=r.original_filename,
                page_start=r.page_start, page_end=r.page_end, heading_path=r.heading_path,
            )
    retrieved = ev.Retrieved(
        hits=tuple(known[h.chunk_id] for h in result.hits),
        vector_ranked=tuple(known[i] for i in result.vector_ranked if i in known),
        keyword_ranked=tuple(known[i] for i in result.keyword_ranked if i in known),
        sufficient=result.sufficient,
        reason=result.reason,
        max_similarity=result.max_similarity,
        keyword_coverage=result.keyword_coverage,
        candidate_count=result.candidate_count,
    )
    return retrieved, result


# ── the command ──────────────────────────────────────────────────────────────


def _k_type(value: str) -> int:
    try:
        k = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if not 1 <= k <= MAX_K:
        raise argparse.ArgumentTypeError(f"k must be between 1 and {MAX_K}")
    return k


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.eval_retrieval",
        description="Retrieval-only evaluation on the golden set (read-only; prints ids, titles, pages and headings, never chunk text).",
    )
    parser.add_argument("--tenant", required=True, help="the tenant slug")
    parser.add_argument("--class-id", required=True, type=uuid.UUID, help="the class to evaluate")
    parser.add_argument("--subject-id", required=True, type=uuid.UUID, help="the subject to evaluate")
    parser.add_argument("--golden", default=DEFAULT_GOLDEN, help=f"the golden-set CSV (default {DEFAULT_GOLDEN})")
    parser.add_argument("--k", type=_k_type, default=10, help=f"hits per question, 1 to {MAX_K} (default 10)")
    parser.add_argument("--report", required=True, help="where to write the JSON report")
    parser.add_argument(
        "--compare", action="store_true",
        help="run the golden set under many retrieval configurations and print one comparison table",
    )
    parser.add_argument(
        "--grid", nargs="+", metavar="NAME=LIST", default=None,
        help="with --compare: a smaller grid, e.g. --grid vw=1,2 df=off,0.3 mt=1 (vw = vector weight, "
             "df = keyword max document frequency, off or 0 to 1, mt = keyword min matched terms; "
             "a name that is left out stays neutral). Default: vw=1,2,3 df=off,0.5,0.3,0.2,0.1 mt=1,2",
    )
    return parser


async def run_golden_set(
    db: AsyncSession, scope: retrieval.RetrievalScope, rows: list[ev.GoldenRow], queries: list[str],
    query_embedders: list[retrieval.QueryEmbedder], config: retrieval.RetrievalConfig, k: int,
    documents: list[ev.DocRef],
) -> tuple[list[ev.QuestionResult], set[str]]:
    """Every question through retrieval.search under ONE configuration. The query embedders already hold the
    question vectors, so a second configuration costs no embedding. Returns the results and the search paths."""
    results: list[ev.QuestionResult] = []
    paths: set[str] = set()
    for row, query, query_embedder in zip(rows, queries, query_embedders, strict=True):
        try:
            retrieved, result = await retrieve_one(db, scope, query, query_embedder, config, k)
        except retrieval.SearchUnavailable as exc:
            raise CommandError(f"golden set row {row.row}: the search failed ({exc.code})") from None
        paths.add(result.path)
        results.append(ev.evaluate_question(row, retrieved, documents))
    return results, paths


def config_for(base: retrieval.RetrievalConfig, point: ev.GridPoint) -> retrieval.RetrievalConfig:
    """The base configuration with the four 5d-4 values of a grid point; everything else (k values, threshold,
    cap) stays as the settings have it."""
    try:
        return dataclasses.replace(
            base, rrf_vector_weight=point.vector_weight, rrf_keyword_weight=point.keyword_weight,
            keyword_max_df=point.keyword_max_df, keyword_min_matched_terms=point.keyword_min_matched_terms,
        )
    except ValueError as exc:
        raise CommandError(f"grid point {point.label}: {exc}") from None


def point_of(config: retrieval.RetrievalConfig) -> ev.GridPoint:
    return ev.GridPoint(
        config.rrf_vector_weight, config.rrf_keyword_weight, config.keyword_max_df, config.keyword_min_matched_terms
    )


async def _run(args: argparse.Namespace, embedder_loader: EmbedderLoader | None) -> int:
    grid: list[ev.GridPoint] = []
    if args.grid is not None and not args.compare:
        raise CommandError("--grid only works together with --compare")
    if args.compare:
        try:
            grid = ev.parse_grid(args.grid) if args.grid is not None else ev.build_grid()
        except ev.GridError as exc:
            raise CommandError(f"bad --grid: {exc}") from None
    golden_path = Path(args.golden)
    if not golden_path.is_file():
        raise Skipped(f"golden file not found: {golden_path} (pass --golden <csv>)")
    try:
        rows = ev.load_golden(golden_path)
    except ev.GoldenSetError as exc:
        raise CommandError(str(exc)) from None
    if embedder_loader is None:
        check_model_available()

    tenant_id = await resolve_tenant(args.tenant)
    if tenant_id is None:
        raise CommandError(f"unknown tenant: {args.tenant}")
    config = retrieval.RetrievalConfig.from_settings()
    k: int = args.k

    async with readonly_tenant_session(tenant_id) as db:
        try:
            scope = await retrieval.RetrievalScope.for_operator(db, tenant_id, args.class_id, args.subject_id)
        except HTTPException:
            raise CommandError(
                "that class and subject are not offered by this tenant (check --class-id and --subject-id)"
            ) from None
        corpus = await read_corpus(db, tenant_id, args.class_id, args.subject_id)
        if corpus["chunks"] == 0:
            raise Skipped(
                f"no published documents with chunks for this class and subject in tenant {args.tenant} "
                "(publish the files first)"
            )

        # Problems in the golden file show up now, before the model is loaded.
        queries: list[str] = []
        for row in rows:
            try:
                query = retrieval.normalize_query(row.question, config.max_query_chars)
                await retrieval.query_lexemes(db, query)
            except retrieval.QueryError as exc:
                raise CommandError(f"golden set row {row.row}: the question cannot be searched ({exc.code})") from None
            queries.append(query)

        print(f"{corpus['chunks']} chunk(s) in {len(corpus['documents'])} published document(s); loading the model ...")
        embedder = await asyncio.to_thread(embedder_loader or _load_real_embedder)
        base_embedder = retrieval.QueryEmbedder.from_embedder(embedder)

        # Each question is embedded ONCE; the embedder object answers for that question only, however many
        # configurations search with it.
        query_embedders: list[retrieval.QueryEmbedder] = []
        for row, query in zip(rows, queries, strict=True):
            try:
                query_embedders.append(await retrieval.embed_query(base_embedder, query))
            except retrieval.SearchUnavailable as exc:
                raise CommandError(f"golden set row {row.row}: the search failed ({exc.code})") from None

        documents = [ev.DocRef(d["title"], d["original_filename"]) for d in corpus["documents"]]
        if args.compare:
            current_point = point_of(config)
            plan: list[tuple[ev.RunRole, str, ev.GridPoint]] = [
                ("current", "current settings", current_point),
                ("baseline", "vector only", ev.vector_only_point()),
                *(("grid", "", point) for point in grid),
            ]
            print(f"comparing {len(plan)} configuration(s) on {len(rows)} question(s), k = {k} ...")
            runs: list[ev.ConfigRun] = []
            compared_paths: set[str] = set()
            for role, label, point in plan:
                run_results, run_paths = await run_golden_set(
                    db, scope, rows, queries, query_embedders, config_for(config, point), k, documents
                )
                compared_paths |= run_paths
                runs.append(ev.ConfigRun(role, label, point, tuple(run_results)))
            return finish_compare(args, golden_path, embedder, config, corpus, runs, sorted(compared_paths), k)

        print(f"running {len(rows)} question(s), k = {k} ...")
        results, paths = await run_golden_set(db, scope, rows, queries, query_embedders, config, k, documents)

    meta: dict[str, Any] = {
        "tenant": args.tenant,
        "class_id": str(args.class_id),
        "subject_id": str(args.subject_id),
        "golden_file": golden_path.name,
        "model": str(embedder.model_id),
        "k": k,
        **threshold_meta(str(embedder.model_id), config),
        "keyword_coverage_threshold": config.min_keyword_coverage or None,
        "keyword_max_candidates": config.keyword_max_candidates,
        "fusion": fusion_settings(config),
        "keyword_side": "on" if retrieval.keyword_side_runs(config) else "off",
        "search_paths": sorted(paths),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": corpus,
    }
    report = ev.build_report(results, cutoff=k, meta=meta)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(ev.format_console(report))
    print(f"report written: {report_path}")
    return 0


def threshold_meta(model_key: str, config: retrieval.RetrievalConfig) -> dict[str, Any]:
    """The threshold, whether it is calibrated for this model (derived, step 5d-5) and, when it is, the record
    of how it was measured (retrieval.CALIBRATION, only when that record is about this model)."""
    calibrated = retrieval.is_threshold_calibrated(model_key, config)
    record = retrieval.CALIBRATION
    return {
        "threshold": config.min_similarity,
        "threshold_calibrated": calibrated,
        "calibrated_model_key": config.calibrated_model_key,
        "calibration": dataclasses.asdict(record) if calibrated and record.model_key == model_key else None,
    }


def fusion_settings(config: retrieval.RetrievalConfig) -> dict[str, Any]:
    """The four 5d-4 settings as the run used them (numbers only)."""
    return {
        "keyword_max_df": config.keyword_max_df, "rrf_vector_weight": config.rrf_vector_weight,
        "rrf_keyword_weight": config.rrf_keyword_weight, "keyword_min_matched_terms": config.keyword_min_matched_terms,
    }


def finish_compare(
    args: argparse.Namespace, golden_path: Path, embedder: Any, config: retrieval.RetrievalConfig,
    corpus: dict[str, Any], runs: list[ev.ConfigRun], paths: list[str], k: int,
) -> int:
    """Write the comparison report (numbers only) and print the table."""
    meta: dict[str, Any] = {
        "kind": "compare",
        "tenant": args.tenant,
        "class_id": str(args.class_id),
        "subject_id": str(args.subject_id),
        "golden_file": golden_path.name,
        "model": str(embedder.model_id),
        "k": k,
        **threshold_meta(str(embedder.model_id), config),
        "current_settings": fusion_settings(config),
        "search_paths": paths,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": {"documents": len(corpus["documents"]), "chunks": corpus["chunks"], "flagged_chunks": corpus["flagged_chunks"]},
    }
    report = ev.build_compare_report(runs, cutoff=k, meta=meta)
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(ev.format_compare(report))
    print(f"report written: {report_path}")
    return 0


async def run(argv: list[str] | None = None, *, embedder_loader: EmbedderLoader | None = None) -> int:
    """The command, for an event loop that already runs (main() runs it with asyncio.run). Returns the exit
    code. `embedder_loader` replaces the real model (tests use a scripted embedder)."""
    args = build_parser().parse_args(argv)
    try:
        return await _run(args, embedder_loader)
    except Skipped as skip:
        print(f"skipped: {skip.reason}")
        return 0
    except CommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


async def _main(argv: list[str] | None) -> int:
    from app.db.engine import engine, platform_engine

    try:
        return await run(argv)
    finally:
        await engine.dispose()
        await platform_engine.dispose()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    sys.exit(main())
