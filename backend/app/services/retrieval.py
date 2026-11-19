"""
app/services/retrieval.py
─────────────────────────
Tenant-safe hybrid retrieval over chunks (step 5d-1, design A5, docs/phase5d-plan.md sections 3-5).
There is no LLM call here: this module only finds and ranks chunks.

Scope
    build_retrieval_scope(db, user, purpose=..., class_id=None, subject_id=None, include_drafts=False)
        THE way API code gets a scope. The pairs come from document_scope.authorized_pairs (always
        (class, subject) PAIRS, never a cross product) and the tenant from the authenticated user. The
        optional ids only narrow those pairs; anything outside them is the generic 404. Students use the
        "read" purpose, which can never include drafts.
    RetrievalScope.for_operator(db, tenant_id, class_id, subject_id)
        For operator scripts only (the 5d-3 evaluation harness): one offered pair of one tenant, published
        versions only. API modules must not use it (a test checks).

Search (all statements in the caller's tenant transaction, as app_tenant, so RLS applies underneath)
    1. the query's lexemes, from the same 'simple' parser as chunks.content_tsv;
    2. the number of candidate chunks in scope (published, or published + ready for drafts; not excluded;
       document not deleted), which picks the exact or the HNSW path;
    3. ONE statement that returns the vector side (cosine, e5 "query: " vector), the keyword side (an OR
       of the query's lexemes, ranked by ts_rank_cd) and every detail of those chunks, so all of it comes
       from one snapshot: a concurrent publish shows the old version or the new one, never a mix.
    Fusion is reciprocal rank fusion in Python. Sufficiency compares the best vector similarity with
    RETRIEVAL_MIN_SIMILARITY (UNCALIBRATED until the 5d-3 sweep), plus an optional keyword-coverage clause.

User text never becomes SQL: the query, its lexemes, the OR tsquery and the vector are bind parameters.
The only values formatted into SQL are the pgvector schema (read from pg_extension, quoted by Postgres)
and, on the HNSW path, the model dimension (an int checked here), plus the module's own constant filter
fragments below. The query text is never logged.
"""

from __future__ import annotations

import asyncio
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.academic import ClassSubject
from app.db.models.document import VERSION_STATUS_PUBLISHED, VERSION_STATUS_READY
from app.db.models.user import User
from app.services import document_scope
from app.services.document_text import clean_text

Pair = tuple[uuid.UUID, uuid.UUID]  # (class_id, subject_id)
Purpose = Literal["manage", "read"]

INSUFFICIENT_MESSAGE = "Not enough information in your class material."
MAX_QUERY_LEXEMES = 64  # the OR query uses at most this many distinct words of the question
RAW_QUERY_FACTOR = 8  # a raw query longer than max_chars * this is refused before cleaning
MAX_VECTOR_DIM = 2000  # pgvector's HNSW limit

REASON_EMPTY_SCOPE = "empty_scope"
REASON_NO_CANDIDATES = "no_candidates"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_SIMILARITY = "similarity"
REASON_KEYWORD_COVERAGE = "keyword_coverage"

_SCOPE_KEY = object()  # only this module (build_retrieval_scope, for_operator) can create a scope
_SCHEMA_RE = re.compile(r'^(?:[a-z_][a-z0-9_$]*|"(?:[^"]|"")+")$')


class QueryError(ValueError):
    """The query cannot be searched; `code` is the API error code (invalid_query, query_too_long)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalConfig:
    vector_k: int = 20
    keyword_k: int = 20
    final_k: int = 6
    rrf_k: int = 60
    max_query_chars: int = 500
    min_similarity: float = 0.85
    min_keyword_coverage: float = 0.0  # 0 = the keyword clause is off
    exact_max_candidates: int = 50_000
    hnsw_ef_search: int = 100
    # False until a value measured by the 5d-3 sweep replaces the default; shown in every response.
    threshold_calibrated: bool = False

    @classmethod
    def from_settings(cls, s: Any = None) -> RetrievalConfig:
        s = s or settings
        return cls(
            vector_k=s.RETRIEVAL_VECTOR_K,
            keyword_k=s.RETRIEVAL_KEYWORD_K,
            final_k=s.RETRIEVAL_FINAL_K,
            rrf_k=s.RETRIEVAL_RRF_K,
            max_query_chars=s.RETRIEVAL_MAX_QUERY_CHARS,
            min_similarity=s.RETRIEVAL_MIN_SIMILARITY,
            min_keyword_coverage=s.RETRIEVAL_MIN_KEYWORD_COVERAGE,
            exact_max_candidates=s.RETRIEVAL_EXACT_MAX_CANDIDATES,
            hnsw_ef_search=s.RETRIEVAL_HNSW_EF_SEARCH,
        )


# ── Scope ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalScope:
    """What one search may reach: a tenant, a set of (class, subject) pairs and the version states."""

    tenant_id: uuid.UUID
    pairs: frozenset[Pair]
    statuses: tuple[str, ...]
    key: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.key is not _SCOPE_KEY:
            raise TypeError("a RetrievalScope is built by build_retrieval_scope (or for_operator in scripts)")

    @property
    def ordered_pairs(self) -> list[Pair]:
        return sorted(self.pairs)

    @classmethod
    async def for_operator(
        cls, db: AsyncSession, tenant_id: uuid.UUID, class_id: uuid.UUID, subject_id: uuid.UUID
    ) -> RetrievalScope:
        """One offered pair of one tenant, published versions only. Operator scripts only, never the API."""
        await _check_session_tenant(db, tenant_id)
        offered = (
            await db.execute(
                select(ClassSubject.class_id).where(
                    ClassSubject.tenant_id == tenant_id,
                    ClassSubject.class_id == class_id,
                    ClassSubject.subject_id == subject_id,
                )
            )
        ).first()
        if offered is None:
            raise document_scope.not_found()
        return cls(tenant_id, frozenset({(class_id, subject_id)}), (VERSION_STATUS_PUBLISHED,), _SCOPE_KEY)


def statuses_for(purpose: Purpose, include_drafts: bool) -> tuple[str, ...]:
    """Version states a scope may reach. Drafts (ready versions) only for teachers and admins (manage)."""
    if include_drafts:
        if purpose != "manage":
            raise ValueError("drafts are never part of a read scope")
        return (VERSION_STATUS_PUBLISHED, VERSION_STATUS_READY)
    return (VERSION_STATUS_PUBLISHED,)


async def _check_session_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    current = (await db.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one_or_none()
    if current != str(tenant_id):
        raise RuntimeError("the session tenant does not match the tenant of the scope")


async def build_retrieval_scope(
    db: AsyncSession,
    user: User,
    *,
    purpose: Purpose,
    class_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    include_drafts: bool = False,
) -> RetrievalScope:
    """The caller's scope, from their own pairs. A class, subject or pair outside them is the generic 404."""
    statuses = statuses_for(purpose, include_drafts)
    await _check_session_tenant(db, user.tenant_id)
    pairs = await document_scope.authorized_pairs(db, user, purpose=purpose)
    narrowed = pairs
    if class_id is not None:
        narrowed = frozenset(p for p in narrowed if p[0] == class_id)
    if subject_id is not None:
        narrowed = frozenset(p for p in narrowed if p[1] == subject_id)
    if (class_id is not None or subject_id is not None) and not narrowed:
        raise document_scope.not_found()
    return RetrievalScope(user.tenant_id, narrowed, statuses, _SCOPE_KEY)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def normalize_query(raw: str, max_chars: int) -> str:
    """The cleaned query (NFC, invisible characters dropped, whitespace collapsed; the ingest rules).
    Empty gives invalid_query; longer than max_chars after cleaning gives query_too_long."""
    if not isinstance(raw, str):
        raise QueryError("invalid_query")
    if len(raw) > max_chars * RAW_QUERY_FACTOR:
        raise QueryError("query_too_long")
    cleaned = clean_text(raw)
    if not cleaned:
        raise QueryError("invalid_query")
    if len(cleaned) > max_chars:
        raise QueryError("query_too_long")
    return cleaned


def _quote_lexeme(lexeme: str) -> str:
    # tsquery input: a backslash escapes the next character and a quote is doubled inside quotes
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def unique_lexemes(lexemes: Sequence[str]) -> list[str]:
    """Distinct non-empty lexemes in their order, at most MAX_QUERY_LEXEMES."""
    out: list[str] = []
    for lx in lexemes:
        if lx and lx not in out:
            out.append(lx)
            if len(out) == MAX_QUERY_LEXEMES:
                break
    return out


def or_tsquery_text(lexemes: Sequence[str]) -> str | None:
    """`'a' | 'b' | ...` for tsquery input (no parser runs on it), or None when there is no lexeme."""
    words = unique_lexemes(lexemes)
    return " | ".join(_quote_lexeme(w) for w in words) if words else None


@dataclass(frozen=True)
class Fused:
    id: uuid.UUID
    score: float
    vector_rank: int | None
    keyword_rank: int | None


def _ranks(ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    ranks: dict[uuid.UUID, int] = {}
    for chunk_id in ids:
        if chunk_id not in ranks:
            ranks[chunk_id] = len(ranks) + 1
    return ranks


def rrf_fuse(vector_ids: Sequence[uuid.UUID], keyword_ids: Sequence[uuid.UUID], rrf_k: int) -> list[Fused]:
    """Reciprocal rank fusion: score = sum of 1 / (rrf_k + rank) over the sides that list the chunk.
    Ties: the better single rank first, then the smaller id (deterministic)."""
    vranks, kranks = _ranks(vector_ids), _ranks(keyword_ids)
    fused = []
    for chunk_id in {*vranks, *kranks}:
        v, k = vranks.get(chunk_id), kranks.get(chunk_id)
        score = (1.0 / (rrf_k + v) if v is not None else 0.0) + (1.0 / (rrf_k + k) if k is not None else 0.0)
        fused.append(Fused(chunk_id, score, v, k))
    fused.sort(key=lambda f: (-f.score, min(r for r in (f.vector_rank, f.keyword_rank) if r is not None), f.id))
    return fused


def decide_sufficiency(
    *,
    has_scope: bool,
    candidate_count: int,
    max_similarity: float | None,
    keyword_coverage: float | None,
    config: RetrievalConfig,
) -> tuple[bool, str]:
    """(sufficient, reason). The keyword clause counts only when min_keyword_coverage > 0."""
    if not has_scope:
        return False, REASON_EMPTY_SCOPE
    if candidate_count == 0:
        return False, REASON_NO_CANDIDATES
    if max_similarity is not None and max_similarity >= config.min_similarity:
        return True, REASON_SIMILARITY
    if config.min_keyword_coverage > 0 and keyword_coverage is not None and keyword_coverage >= config.min_keyword_coverage:
        return True, REASON_KEYWORD_COVERAGE
    return False, REASON_BELOW_THRESHOLD


# ── The query embedder ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueryEmbedder:
    """How a search turns the cleaned query into a vector, and which stored vectors it compares with."""

    model_key: str
    dim: int
    embed: Callable[[str], Awaitable[list[float]]]

    @classmethod
    def from_embedder(cls, embedder: Any) -> QueryEmbedder:
        """Wrap an Embedder (real or fake); embed_query runs in a thread (it adds the "query: " prefix)."""

        async def embed(query: str) -> list[float]:
            vector: list[float] = await asyncio.to_thread(embedder.embed_query, query)
            return vector

        return cls(model_key=str(embedder.model_id), dim=int(embedder.dim), embed=embed)


# ── Results ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hit:
    rank: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    version_id: uuid.UUID
    version_no: int
    version_status: str
    original_filename: str
    page_start: int | None
    page_end: int | None
    heading_path: str | None
    content: str
    vector_rank: int | None
    vector_similarity: float | None
    keyword_rank: int | None
    keyword_matched_terms: int | None
    fused_score: float
    injection_flags: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.injection_flags)


@dataclass(frozen=True)
class RetrievalResult:
    sufficient: bool
    reason: str
    message: str | None
    threshold: float
    threshold_calibrated: bool
    keyword_coverage_threshold: float | None
    model: str
    candidate_count: int
    path: str  # "none", "exact" or "hnsw"
    max_similarity: float | None
    keyword_coverage: float | None
    query_lexeme_count: int
    hits: tuple[Hit, ...]
    vector_ranked: tuple[uuid.UUID, ...]  # the vector side alone, best first (for tests and the harness)
    keyword_ranked: tuple[uuid.UUID, ...]  # the keyword side alone, best first


# ── SQL ──────────────────────────────────────────────────────────────────────
# The filter fragments are module constants on purpose: the prove-it-fails tests switch each one off
# (tests/security/test_prove_it_fails_retrieval.py). They are read when a statement is built, never cached.

TENANT_FILTER = "{alias}.tenant_id = CAST(:t AS uuid)"
PAIR_MATCH = "s.class_id = dc.class_id AND s.subject_id = dc.subject_id"
STATUS_FILTER = "v.status = ANY(CAST(:statuses AS text[]))"
DELETED_FILTER = "d.deleted_at IS NULL"
EXCLUDED_FILTER = "NOT c.excluded"

VECTOR_SCHEMA_SQL = (
    "SELECT format('%I', n.nspname) FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
    "WHERE e.extname = 'vector'"
)
LEXEMES_SQL = "SELECT tsvector_to_array(to_tsvector('simple', :q))"

_SCOPE_CTES = """
WITH scope(class_id, subject_id) AS (
    SELECT * FROM unnest(CAST(:class_ids AS uuid[]), CAST(:subject_ids AS uuid[]))
), docs AS MATERIALIZED (
    SELECT d.id AS document_id
    FROM app.documents d
    WHERE {t_d} AND {deleted}
      AND EXISTS (
          SELECT 1 FROM app.document_classes dc, scope s
          WHERE dc.tenant_id = d.tenant_id AND dc.document_id = d.id AND dc.subject_id = d.subject_id
            AND {pair_match}
      )
), cand AS MATERIALIZED (
    SELECT c.id
    FROM app.chunks c
    JOIN docs ON docs.document_id = c.document_id
    JOIN app.document_versions v ON v.tenant_id = c.tenant_id AND v.id = c.document_version_id
    WHERE {t_c} AND {t_v} AND {status} AND {excluded}
)"""

_COUNT_TAIL = "\nSELECT count(*) FROM cand"

# Exact path: cosine distance to every candidate vector of the active model.
_VEC_EXACT = """, vec AS (
    SELECT x.id, row_number() OVER (ORDER BY x.dist, x.id) AS rnk, 1 - x.dist AS sim
    FROM (
        SELECT e.chunk_id AS id, e.embedding OPERATOR({schema}.<=>) CAST(CAST(:qv AS text) AS {schema}.vector) AS dist
        FROM cand JOIN app.chunk_embeddings e ON e.chunk_id = cand.id
        WHERE {t_e} AND e.embedding_model = :model AND e.embedding_dim = :dim
        ORDER BY dist, e.chunk_id
        LIMIT :vk
    ) x
)"""

# HNSW path: the expression and predicate of the partial index (migration 0008), candidates as a filter.
# relaxed_order may return rows slightly out of order, so the outer row_number sorts them again.
_VEC_HNSW = """, vec AS (
    SELECT x.id, row_number() OVER (ORDER BY x.dist, x.id) AS rnk, 1 - x.dist AS sim
    FROM (
        SELECT e.chunk_id AS id,
               CAST(e.embedding AS {schema}.vector({dim})) OPERATOR({schema}.<=>) CAST(CAST(:qv AS text) AS {schema}.vector({dim})) AS dist
        FROM app.chunk_embeddings e
        WHERE {t_e} AND e.embedding_model = :model AND e.embedding_dim = :dim
          AND EXISTS (SELECT 1 FROM cand WHERE cand.id = e.chunk_id)
        ORDER BY CAST(e.embedding AS {schema}.vector({dim})) OPERATOR({schema}.<=>) CAST(CAST(:qv AS text) AS {schema}.vector({dim}))
        LIMIT :vk
    ) x
)"""

_KW_AND_DETAILS = """, kw AS (
    SELECT y.id, row_number() OVER (ORDER BY y.score DESC, y.id) AS rnk
    FROM (
        SELECT c.id, ts_rank_cd(c.content_tsv, q.q, 1) AS score
        FROM cand JOIN app.chunks c ON c.id = cand.id, (SELECT CAST(CAST(:tsq AS text) AS tsquery) AS q) q
        WHERE {t_c} AND c.content_tsv @@ q.q
        ORDER BY score DESC, c.id
        LIMIT :kk
    ) y
), ids AS (
    SELECT id FROM vec UNION SELECT id FROM kw
)
SELECT c.id, c.document_id, d.title, c.document_version_id, v.version_no, v.status, v.original_filename,
       c.page_start, c.page_end, c.heading_path, c.content, c.injection_flags,
       vec.rnk AS vector_rank, vec.sim AS vector_similarity, kw.rnk AS keyword_rank,
       CASE WHEN kw.id IS NULL THEN NULL ELSE cardinality(ARRAY(
           SELECT unnest(tsvector_to_array(c.content_tsv)) INTERSECT SELECT unnest(CAST(:lexemes AS text[]))
       )) END AS matched
FROM ids
JOIN app.chunks c ON c.id = ids.id
JOIN app.document_versions v ON v.tenant_id = c.tenant_id AND v.id = c.document_version_id
JOIN app.documents d ON d.tenant_id = c.tenant_id AND d.id = c.document_id
LEFT JOIN vec ON vec.id = ids.id
LEFT JOIN kw ON kw.id = ids.id
WHERE {t_c} AND {t_v} AND {t_d} AND {status} AND {excluded} AND {deleted}"""


def _filters() -> dict[str, str]:
    return {
        "t_d": TENANT_FILTER.format(alias="d"),
        "t_c": TENANT_FILTER.format(alias="c"),
        "t_v": TENANT_FILTER.format(alias="v"),
        "t_e": TENANT_FILTER.format(alias="e"),
        "pair_match": PAIR_MATCH,
        "status": STATUS_FILTER,
        "deleted": DELETED_FILTER,
        "excluded": EXCLUDED_FILTER,
    }


def count_sql() -> str:
    return (_SCOPE_CTES + _COUNT_TAIL).format(**_filters())


def search_sql(schema: str, *, hnsw: bool, dim: int) -> str:
    """The search statement. `schema` comes from pg_extension (quoted by format('%I')); `dim` is an int."""
    if not _SCHEMA_RE.match(schema):
        raise RuntimeError("unexpected pgvector schema name")
    if not isinstance(dim, int) or isinstance(dim, bool) or not 1 <= dim <= MAX_VECTOR_DIM:
        raise RuntimeError("the embedding dimension must be an int between 1 and 2000")
    template = _SCOPE_CTES + (_VEC_HNSW if hnsw else _VEC_EXACT) + _KW_AND_DETAILS
    return template.format(schema=schema, dim=dim, **_filters())


def _vector_literal(vector: Sequence[float], dim: int) -> str:
    if len(vector) != dim or not all(math.isfinite(x) for x in vector):
        raise RuntimeError("the query embedder returned a vector of the wrong size or value")
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


# ── Search ───────────────────────────────────────────────────────────────────


def _result(
    config: RetrievalConfig, model: str, *, has_scope: bool, candidate_count: int = 0, path: str = "none",
    max_similarity: float | None = None, keyword_coverage: float | None = None, lexeme_count: int = 0,
    hits: tuple[Hit, ...] = (), vector_ranked: tuple[uuid.UUID, ...] = (), keyword_ranked: tuple[uuid.UUID, ...] = (),
) -> RetrievalResult:
    sufficient, reason = decide_sufficiency(
        has_scope=has_scope, candidate_count=candidate_count, max_similarity=max_similarity,
        keyword_coverage=keyword_coverage, config=config,
    )
    return RetrievalResult(
        sufficient=sufficient,
        reason=reason,
        message=None if sufficient else INSUFFICIENT_MESSAGE,
        threshold=config.min_similarity,
        threshold_calibrated=config.threshold_calibrated,
        keyword_coverage_threshold=config.min_keyword_coverage or None,
        model=model,
        candidate_count=candidate_count,
        path=path,
        max_similarity=max_similarity,
        keyword_coverage=keyword_coverage,
        query_lexeme_count=lexeme_count,
        hits=hits,
        vector_ranked=vector_ranked,
        keyword_ranked=keyword_ranked,
    )


async def search(
    db: AsyncSession,
    scope: RetrievalScope,
    raw_query: str,
    query_embedder: QueryEmbedder,
    config: RetrievalConfig,
    *,
    k: int | None = None,
) -> RetrievalResult:
    """Hybrid search inside `scope` (see the module docstring). Raises QueryError for an unusable query."""
    query = normalize_query(raw_query, config.max_query_chars)
    final_k = k or config.final_k
    model = query_embedder.model_key
    if not scope.pairs:
        return _result(config, model, has_scope=False)

    pairs = scope.ordered_pairs
    scope_params: dict[str, Any] = {
        "t": scope.tenant_id,
        "class_ids": [p[0] for p in pairs],
        "subject_ids": [p[1] for p in pairs],
        "statuses": list(scope.statuses),
    }
    count = int((await db.execute(text(count_sql()), scope_params)).scalar_one())
    if count == 0:
        return _result(config, model, has_scope=True)

    lexemes = unique_lexemes(list((await db.execute(text(LEXEMES_SQL), {"q": query})).scalar_one() or []))
    vector = _vector_literal(await query_embedder.embed(query), query_embedder.dim)
    schema = (await db.execute(text(VECTOR_SCHEMA_SQL))).scalar_one_or_none()
    if schema is None:
        raise RuntimeError("the pgvector extension is not installed in this database")

    hnsw = count > config.exact_max_candidates
    if hnsw:
        await db.execute(
            text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true), set_config('hnsw.ef_search', :ef, true)"),
            {"ef": str(config.hnsw_ef_search)},
        )
    rows = (
        await db.execute(
            text(search_sql(schema, hnsw=hnsw, dim=query_embedder.dim)),
            {
                **scope_params,
                "qv": vector,
                "model": model,
                "dim": query_embedder.dim,
                "vk": config.vector_k,
                "tsq": or_tsquery_text(lexemes),
                "lexemes": lexemes,
                "kk": config.keyword_k,
            },
        )
    ).mappings().all()

    by_id = {row["id"]: row for row in rows}
    vector_ranked = tuple(r["id"] for r in sorted((r for r in rows if r["vector_rank"] is not None), key=lambda r: r["vector_rank"]))
    keyword_ranked = tuple(r["id"] for r in sorted((r for r in rows if r["keyword_rank"] is not None), key=lambda r: r["keyword_rank"]))
    fused = rrf_fuse(vector_ranked, keyword_ranked, config.rrf_k)[:final_k]

    hits = []
    for position, f in enumerate(fused, start=1):
        row = by_id[f.id]
        sim = row["vector_similarity"]
        hits.append(
            Hit(
                rank=position,
                chunk_id=row["id"],
                document_id=row["document_id"],
                document_title=row["title"],
                version_id=row["document_version_id"],
                version_no=row["version_no"],
                version_status=row["status"],
                original_filename=row["original_filename"],
                page_start=row["page_start"],
                page_end=row["page_end"],
                heading_path=row["heading_path"],
                content=row["content"],
                vector_rank=f.vector_rank,
                vector_similarity=float(sim) if sim is not None else None,
                keyword_rank=f.keyword_rank,
                keyword_matched_terms=row["matched"],
                fused_score=f.score,
                injection_flags=tuple(row["injection_flags"] or ()),
            )
        )

    sims = [float(r["vector_similarity"]) for r in rows if r["vector_similarity"] is not None]
    matched = [int(r["matched"]) for r in rows if r["matched"] is not None]
    coverage = (max(matched) / len(lexemes)) if (matched and lexemes) else None
    return _result(
        config, model, has_scope=True, candidate_count=count, path="hnsw" if hnsw else "exact",
        max_similarity=max(sims) if sims else None, keyword_coverage=coverage, lexeme_count=len(lexemes),
        hits=tuple(hits), vector_ranked=vector_ranked, keyword_ranked=keyword_ranked,
    )
