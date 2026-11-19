"""
app/services/retrieval.py
─────────────────────────
Tenant-safe hybrid retrieval over chunks (step 5d-1, hardened in 5d-1b; design A5, docs/phase5d-plan.md
sections 3-5 and 14). There is no LLM call here: this module only finds and ranks chunks.

Scope (a RetrievalScope can only be issued by this module)
    build_retrieval_scope(db, user, purpose=..., class_id=None, subject_id=None, include_drafts=False)
        THE way API code gets a scope. The pairs come from document_scope.authorized_pairs (always
        (class, subject) PAIRS, never a cross product) and the tenant from the authenticated user. The
        optional ids only narrow those pairs (scope.narrow); anything outside them is the generic 404.
        Students use the "read" purpose, which can never include drafts.
    RetrievalScope.for_operator(db, tenant_id, class_id, subject_id)
        For operator scripts only (the 5d-3 evaluation harness): one offered pair of one tenant, published
        versions only, purpose "read". Only app/scripts may use it (a test scans app/).
    A scope cannot be made any other way: the constructor, dataclasses.replace, copy, deepcopy and pickle
    all raise, and search() refuses any scope object this module did not issue.

Entry points
    search(db, scope, ...)           read or manage scope; the test search of 5d-2 (scores for a teacher).
    search_for_chat(db, scope, ...)  THE entry point for the chat layer (Phase 6): read scope only, and NO
                                     hits at all when the material is not sufficient.

Search (all statements in the caller's tenant transaction, as app_tenant, so RLS applies underneath)
    0. the session's tenant must be the scope's tenant; statement_timeout is set for the transaction
       (RETRIEVAL_STATEMENT_TIMEOUT_MS) and the caller's value is restored after the search; a statement
       over the limit is SearchUnavailable("search_timeout");
    1. the query's lexemes, from the same 'simple' parser as chunks.content_tsv; none is invalid_query
       (before anything is embedded);
    2. the number of candidate chunks in scope (published, or published + ready for drafts; not excluded;
       document not deleted), which picks the exact or the HNSW path;
    3. ONE statement that returns the vector side (cosine, e5 "query: " vector, NaN distances excluded),
       the keyword side (an OR of the query's lexemes over at most RETRIEVAL_KEYWORD_MAX_CANDIDATES
       matching chunks, ranked by ts_rank_cd) and every detail of those chunks. It reads one snapshot, so
       a publish that commits during a search shows the old version or the new one, never a mix.
    Fusion is reciprocal rank fusion in Python. The top vector-ranked chunk is always kept in the hits,
    and sufficiency is decided on the RETURNED hits only: the best vector similarity against
    RETRIEVAL_MIN_SIMILARITY (UNCALIBRATED until the 5d-3 sweep), plus an optional keyword-coverage clause.

HNSW settings: hnsw.iterative_scan and hnsw.ef_search are set with set_config(..., true), so they last until
the end of the caller's transaction and never leak to the next user of the pooled connection. The value of
hnsw.iterative_scan is read back and must be relaxed_order (pgvector 0.8.0 or newer, also checked).

User text never becomes SQL: the query, its lexemes, the OR tsquery and the vector are bind parameters.
The only values formatted into SQL are the pgvector schema (read from pg_extension, quoted by Postgres)
and, on the HNSW path, the model dimension (an int checked here), plus the module's own constant filter
fragments below. The query text is never logged and never appears in an error.

EVERY text field of a Hit is UNTRUSTED (content, heading_path, document_title, original_filename): it was
written by whoever uploaded the file. flagged=False only means the 5d-2 scanner found nothing; it does not
mean the text is safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import re
import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.academic import ClassSubject
from app.db.models.document import VERSION_STATUS_PUBLISHED, VERSION_STATUS_READY
from app.db.models.user import User
from app.services import document_scope
from app.services.document_text import clean_text

Pair = tuple[uuid.UUID, uuid.UUID]  # (class_id, subject_id)
Purpose = Literal["manage", "read"]
PURPOSES: tuple[Purpose, ...] = ("manage", "read")

INSUFFICIENT_MESSAGE = "Not enough information in your class material."
MAX_QUERY_LEXEMES = 64  # the OR query uses at most this many distinct words of the question
RAW_QUERY_FACTOR = 8  # a raw query longer than max_chars * this is refused before cleaning
MAX_VECTOR_DIM = 2000  # pgvector's HNSW limit
MAX_FINAL_K = 50  # the most hits one search may return (k)
MIN_PGVECTOR_FOR_HNSW = (0, 8, 0)  # hnsw.iterative_scan exists from pgvector 0.8.0
HNSW_ITERATIVE_SCAN = "relaxed_order"
SQLSTATE_QUERY_CANCELED = "57014"  # statement_timeout (or a cancel request)

REASON_EMPTY_SCOPE = "empty_scope"
REASON_NO_CANDIDATES = "no_candidates"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_SIMILARITY = "similarity"
REASON_KEYWORD_COVERAGE = "keyword_coverage"

_SCHEMA_RE = re.compile(r'^(?:[a-z_][a-z0-9_$]*|"(?:[^"]|"")+")$')


class QueryError(ValueError):
    """The query cannot be searched; `code` is the API error code (invalid_query, query_too_long)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SearchUnavailable(RuntimeError):  # noqa: N818 (a domain error named like the API's 503 codes)
    """The search cannot run now (HTTP 503). `code` is search_timeout (a statement ran over
    RETRIEVAL_STATEMENT_TIMEOUT_MS), search_unavailable (the query embedder gave an unusable vector) or
    search_busy (the query-embedding cap stayed full for RETRIEVAL_EMBED_QUEUE_SECONDS).
    The message is the code alone: never SQL, parameters or query text."""

    http_status = 503

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
    statement_timeout_ms: int = 5000
    keyword_max_candidates: int = 2000
    # False until a value measured by the 5d-3 sweep replaces the default; shown in every response.
    threshold_calibrated: bool = False

    def __post_init__(self) -> None:
        if not 1 <= self.final_k <= MAX_FINAL_K:
            raise ValueError(f"final_k must be between 1 and {MAX_FINAL_K}")
        if self.hnsw_ef_search < self.vector_k:
            raise ValueError("hnsw_ef_search must be at least vector_k (the index returns at most ef_search rows)")
        if self.keyword_max_candidates < self.keyword_k:
            raise ValueError("keyword_max_candidates must be at least keyword_k")
        if self.statement_timeout_ms < 1:
            raise ValueError("statement_timeout_ms must be at least 1 (0 would switch the timeout off)")

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
            statement_timeout_ms=s.RETRIEVAL_STATEMENT_TIMEOUT_MS,
            keyword_max_candidates=s.RETRIEVAL_KEYWORD_MAX_CANDIDATES,
        )


# ── Scope ────────────────────────────────────────────────────────────────────

# Set only while _issue_scope builds a scope. __post_init__ refuses to run without it, which also stops
# dataclasses.replace (it calls __init__). _ISSUED holds every scope this module issued (by identity), and
# search() refuses any other object, also one made with object.__new__.
_ISSUING: ContextVar[bool] = ContextVar("retrieval_scope_issuing", default=False)
_ISSUED: weakref.WeakSet[RetrievalScope] = weakref.WeakSet()


@dataclass(frozen=True, eq=False)
class RetrievalScope:
    """What one search may reach: a tenant, a set of (class, subject) pairs, the version states and the
    purpose ("read": students and the chat layer; "manage": teachers and admins in test search)."""

    tenant_id: uuid.UUID
    pairs: frozenset[Pair]
    statuses: tuple[str, ...]
    purpose: Purpose

    def __post_init__(self) -> None:
        if not _ISSUING.get():
            raise TypeError("a RetrievalScope is built by build_retrieval_scope (or for_operator in scripts)")
        if self.purpose not in PURPOSES:
            raise ValueError("purpose must be 'read' or 'manage'")

    def __copy__(self) -> NoReturn:
        raise TypeError("a RetrievalScope cannot be copied; build a new one")

    def __deepcopy__(self, memo: dict[int, Any]) -> NoReturn:
        raise TypeError("a RetrievalScope cannot be copied; build a new one")

    def __reduce_ex__(self, protocol: Any) -> NoReturn:
        raise TypeError("a RetrievalScope cannot be pickled")

    @property
    def ordered_pairs(self) -> list[Pair]:
        return sorted(self.pairs)

    def narrow(self, class_id: uuid.UUID | None = None, subject_id: uuid.UUID | None = None) -> RetrievalScope:
        """The pairs of this scope that match the given class and/or subject: an intersection, never more.
        A narrowing that leaves nothing is the generic 404 (the caller asked for something it does not own)."""
        if class_id is None and subject_id is None:
            return self
        narrowed = frozenset(
            p for p in self.pairs
            if (class_id is None or p[0] == class_id) and (subject_id is None or p[1] == subject_id)
        )
        if not narrowed:
            raise document_scope.not_found()
        return _issue_scope(self.tenant_id, narrowed, self.statuses, self.purpose)

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
        return _issue_scope(tenant_id, frozenset({(class_id, subject_id)}), statuses_for("read", False), "read")


def _issue_scope(tenant_id: uuid.UUID, pairs: frozenset[Pair], statuses: tuple[str, ...], purpose: Purpose) -> RetrievalScope:
    token = _ISSUING.set(True)
    try:
        scope = RetrievalScope(tenant_id, frozenset(pairs), tuple(statuses), purpose)
    finally:
        _ISSUING.reset(token)
    _ISSUED.add(scope)
    return scope


def _require_issued(scope: RetrievalScope) -> None:
    if scope not in _ISSUED:
        raise TypeError("search needs a scope issued by build_retrieval_scope (or for_operator in scripts)")


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
    return _issue_scope(user.tenant_id, pairs, statuses, purpose).narrow(class_id, subject_id)


# ── Pure helpers ─────────────────────────────────────────────────────────────


def normalize_query(raw: str, max_chars: int) -> str:
    """The cleaned query (NFC; control, zero-width and bidi characters and lone surrogates dropped;
    whitespace collapsed; the ingest rules). Empty gives invalid_query; longer than max_chars after cleaning
    gives query_too_long."""
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


def final_k_for(k: int | None, config: RetrievalConfig) -> int:
    """The number of hits to return: `k` (an int from 1 to MAX_FINAL_K) or the configured final_k."""
    if k is None:
        return config.final_k
    if not isinstance(k, int) or isinstance(k, bool) or not 1 <= k <= MAX_FINAL_K:
        raise ValueError(f"k must be an int between 1 and {MAX_FINAL_K}")
    return k


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


def select_hits(fused: Sequence[Fused], vector_ids: Sequence[uuid.UUID], final_k: int) -> list[Fused]:
    """The first `final_k` fused chunks, with the top vector-ranked chunk always among them: if fusion
    ranked it lower, it replaces the lowest fused hit (it then stays last, because its fused score is
    below every other hit's). So the similarity the sufficiency decision uses is always on screen."""
    top = list(fused[:final_k])
    if vector_ids and all(f.id != vector_ids[0] for f in top):
        best = next(f for f in fused if f.id == vector_ids[0])
        top = [*top[: final_k - 1], best]
    return top


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
        """Wrap an Embedder (real or fake); embed_query runs on the query-embedding thread pool (it adds the
        "query: " prefix). Callers embed through embed_query() below, which applies the process-wide cap and
        runs with no transaction open."""

        async def embed(query: str) -> list[float]:
            loop = asyncio.get_running_loop()
            vector: list[float] = await loop.run_in_executor(_query_embed_executor(), embedder.embed_query, query)
            return vector

        return cls(model_key=str(embedder.model_id), dim=int(embedder.dim), embed=embed)


class EmbedLimiter:
    """The process-wide cap on query embeddings (review of 5d-2, finding 5): at most `concurrency` run at once.
    A caller over the cap waits up to `queue_seconds` for a place, then gets SearchUnavailable("search_busy")
    (503); with queue_seconds 0 it is refused at once. `waiting` counts callers waiting for a place now."""

    def __init__(self, concurrency: int, queue_seconds: float) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.concurrency = concurrency
        self.queue_seconds = queue_seconds
        self.waiting = 0
        self._semaphore = asyncio.Semaphore(concurrency)

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._semaphore.locked() and self.queue_seconds <= 0:
            raise SearchUnavailable("search_busy")
        self.waiting += 1
        try:
            async with asyncio.timeout(self.queue_seconds):
                await self._semaphore.acquire()
        except TimeoutError:
            raise SearchUnavailable("search_busy") from None
        finally:
            self.waiting -= 1
        try:
            yield
        finally:
            self._semaphore.release()


_embed_limiter: EmbedLimiter | None = None
_embed_executor: ThreadPoolExecutor | None = None


def get_embed_limiter() -> EmbedLimiter:
    """The process-wide limiter, built from RETRIEVAL_EMBED_CONCURRENCY and RETRIEVAL_EMBED_QUEUE_SECONDS."""
    global _embed_limiter
    if _embed_limiter is None:
        _embed_limiter = EmbedLimiter(settings.RETRIEVAL_EMBED_CONCURRENCY, settings.RETRIEVAL_EMBED_QUEUE_SECONDS)
    return _embed_limiter


def reset_embed_limiter() -> None:
    """Forget the limiter, so the next call reads the settings again (tests)."""
    global _embed_limiter
    _embed_limiter = None


def _query_embed_executor() -> ThreadPoolExecutor:
    """The threads the blocking query model runs on: a pool of its own (not asyncio's shared default pool), as
    large as the cap. Created on first use, kept for the life of the process."""
    global _embed_executor
    if _embed_executor is None:
        _embed_executor = ThreadPoolExecutor(
            max_workers=settings.RETRIEVAL_EMBED_CONCURRENCY, thread_name_prefix="query-embed"
        )
    return _embed_executor


async def embed_query(query_embedder: QueryEmbedder, query: str) -> QueryEmbedder:
    """Embed the cleaned `query` under the process-wide cap, with NO transaction open (the caller opens its
    search transaction afterwards). Returns a QueryEmbedder that answers with this vector for this query only,
    for search() / search_for_chat(). Raises SearchUnavailable("search_busy") over the cap and whatever the
    embedder raises."""
    async with get_embed_limiter().slot():
        vector = list(await query_embedder.embed(query))

    async def answer(asked: str) -> list[float]:
        if asked != query:
            raise SearchUnavailable("search_unavailable")  # never a vector for another text
        return list(vector)

    return QueryEmbedder(model_key=query_embedder.model_key, dim=query_embedder.dim, embed=answer)


# ── Results ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hit:
    """One returned chunk. UNTRUSTED TEXT: content, heading_path, document_title and original_filename all
    come from an uploaded file or its uploader and may hold instructions aimed at a model. The chat layer
    must wrap every one of them as data. `flagged` and `injection_flags` cover the CHUNK TEXT only (heading
    path and content, scanned at ingest); the title and the file name are not part of them (the test-search
    and flags routes scan those when they answer, nothing else does). flagged=False does NOT mean safe: the
    scanner (5d-2) is a tripwire with certain false negatives."""

    rank: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str  # untrusted
    version_id: uuid.UUID
    version_no: int
    version_status: str
    original_filename: str  # untrusted
    page_start: int | None
    page_end: int | None
    heading_path: str | None  # untrusted
    content: str  # untrusted
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
    max_similarity: float | None  # the best vector similarity among the returned hits
    keyword_coverage: float | None  # the best keyword coverage among the returned hits
    query_lexeme_count: int
    hits: tuple[Hit, ...]
    vector_ranked: tuple[uuid.UUID, ...]  # the vector side alone, best first (for tests and the harness)
    keyword_ranked: tuple[uuid.UUID, ...]  # the keyword side alone, best first


def hit_stats(hits: Sequence[Hit], lexeme_count: int) -> tuple[float | None, float | None]:
    """(max_similarity, keyword_coverage) of the returned hits only; non-finite similarities are dropped."""
    sims = [h.vector_similarity for h in hits if h.vector_similarity is not None and math.isfinite(h.vector_similarity)]
    matched = [h.keyword_matched_terms for h in hits if h.keyword_matched_terms is not None]
    coverage = (max(matched) / lexeme_count) if (matched and lexeme_count) else None
    return (max(sims) if sims else None), coverage


# ── SQL ──────────────────────────────────────────────────────────────────────
# The filter fragments are module constants on purpose: the prove-it-fails tests switch each one off
# (tests/security/test_prove_it_fails_retrieval.py). They are read when a statement is built, never cached.

TENANT_FILTER = "{alias}.tenant_id = CAST(:t AS uuid)"
PAIR_MATCH = "s.class_id = dc.class_id AND s.subject_id = dc.subject_id"
STATUS_FILTER = "v.status = ANY(CAST(:statuses AS text[]))"
DELETED_FILTER = "d.deleted_at IS NULL"
EXCLUDED_FILTER = "NOT c.excluded"

VECTOR_EXTENSION_SQL = (
    "SELECT format('%I', n.nspname), e.extversion FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace "
    "WHERE e.extname = 'vector'"
)
LEXEMES_SQL = "SELECT tsvector_to_array(to_tsvector('simple', :q))"
SHOW_STATEMENT_TIMEOUT_SQL = "SELECT current_setting('statement_timeout')"
SET_STATEMENT_TIMEOUT_SQL = "SELECT set_config('statement_timeout', :ms, true)"
# Transaction-local (third argument true). Returns the value of hnsw.iterative_scan as Postgres now sees it.
HNSW_SETTINGS_SQL = (
    "SELECT set_config('hnsw.iterative_scan', :mode, true), set_config('hnsw.ef_search', :ef, true), "
    "current_setting('hnsw.iterative_scan', true)"
)

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

# Exact path: cosine distance to every candidate vector of the active model. A zero vector has a NaN
# distance (Postgres sorts NaN after every number); it is dropped before the limit.
_VEC_EXACT = """, vec AS (
    SELECT x.id, row_number() OVER (ORDER BY x.dist, x.id) AS rnk, 1 - x.dist AS sim
    FROM (
        SELECT z.id, z.dist
        FROM (
            SELECT e.chunk_id AS id, e.embedding OPERATOR({schema}.<=>) CAST(CAST(:qv AS text) AS {schema}.vector) AS dist
            FROM cand JOIN app.chunk_embeddings e ON e.chunk_id = cand.id
            WHERE {t_e} AND e.embedding_model = :model AND e.embedding_dim = :dim
        ) z
        WHERE z.dist <> CAST('NaN' AS float8)
        ORDER BY z.dist, z.id
        LIMIT :vk
    ) x
)"""

# HNSW path: the expression and predicate of the partial index (migration 0008), candidates as a filter.
# relaxed_order may return rows slightly out of order, so the outer row_number sorts them again. NaN
# distances are dropped after the index scan (a filter on the ordering expression inside would be
# computed twice per scanned row); a zero vector can at most take one of the vector_k places.
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
    WHERE x.dist <> CAST('NaN' AS float8)
)"""

# The keyword side ranks at most :kcap matching candidates (the lowest chunk ids: deterministic, and cheap
# next to ts_rank_cd), so a query of common words cannot rank every chunk of a tenant.
_KW_AND_DETAILS = """, kw AS (
    SELECT y.id, row_number() OVER (ORDER BY y.score DESC, y.id) AS rnk
    FROM (
        SELECT m.id, ts_rank_cd(m.content_tsv, m.q, 1) AS score
        FROM (
            SELECT c.id, c.content_tsv, q.q
            FROM cand JOIN app.chunks c ON c.id = cand.id, (SELECT CAST(CAST(:tsq AS text) AS tsquery) AS q) q
            WHERE {t_c} AND c.content_tsv @@ q.q
            ORDER BY c.id
            LIMIT :kcap
        ) m
        ORDER BY score DESC, m.id
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
    """The query vector as pgvector text. A vector of the wrong size, with NaN or infinity, or with a zero
    norm (no direction: every cosine would be NaN) is search_unavailable."""
    if len(vector) != dim or not all(math.isfinite(x) for x in vector):
        raise SearchUnavailable("search_unavailable")
    norm = math.sqrt(math.fsum(float(x) * float(x) for x in vector))
    if not math.isfinite(norm) or norm < 1e-12:
        raise SearchUnavailable("search_unavailable")
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _version_tuple(version: str | None) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version or "")[:3])


def _is_statement_timeout(exc: DBAPIError) -> bool:
    orig = exc.orig
    cause = getattr(orig, "__cause__", None)
    codes = {getattr(orig, "sqlstate", None), getattr(orig, "pgcode", None), getattr(cause, "sqlstate", None)}
    return SQLSTATE_QUERY_CANCELED in codes


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


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


async def query_lexemes(db: AsyncSession, query: str) -> list[str]:
    """The distinct words of a CLEANED query, from the same 'simple' parser as chunks.content_tsv. None at
    all ("???") is invalid_query: there is nothing to search, so nothing is embedded. The test-search route
    calls this before it loads the model; search() calls it again as its own first step."""
    lexemes = unique_lexemes(list((await db.execute(text(LEXEMES_SQL), {"q": query})).scalar_one() or []))
    if not lexemes:
        raise QueryError("invalid_query")
    return lexemes


async def search(
    db: AsyncSession,
    scope: RetrievalScope,
    raw_query: str,
    query_embedder: QueryEmbedder,
    config: RetrievalConfig,
    *,
    k: int | None = None,
) -> RetrievalResult:
    """Hybrid search inside `scope` (see the module docstring). Raises QueryError for an unusable query,
    ValueError for a k outside 1..50, SearchUnavailable for a timeout or an unusable query vector, and
    RuntimeError when the session is not the scope's tenant."""
    _require_issued(scope)
    await _check_session_tenant(db, scope.tenant_id)
    query = normalize_query(raw_query, config.max_query_chars)
    final_k = final_k_for(k, config)

    previous_timeout = (await db.execute(text(SHOW_STATEMENT_TIMEOUT_SQL))).scalar_one()
    await db.execute(text(SET_STATEMENT_TIMEOUT_SQL), {"ms": str(config.statement_timeout_ms)})
    try:
        result = await _search_in_transaction(db, scope, query, query_embedder, config, final_k)
    except DBAPIError as exc:
        # The transaction is aborted now; the caller's session context rolls it back.
        if _is_statement_timeout(exc):
            raise SearchUnavailable("search_timeout") from None
        raise
    except (QueryError, SearchUnavailable):
        await db.execute(text(SET_STATEMENT_TIMEOUT_SQL), {"ms": previous_timeout})
        raise
    await db.execute(text(SET_STATEMENT_TIMEOUT_SQL), {"ms": previous_timeout})
    return result


async def _search_in_transaction(
    db: AsyncSession, scope: RetrievalScope, query: str, query_embedder: QueryEmbedder, config: RetrievalConfig,
    final_k: int,
) -> RetrievalResult:
    model = query_embedder.model_key
    lexemes = await query_lexemes(db, query)
    if not scope.pairs:
        return _result(config, model, has_scope=False, lexeme_count=len(lexemes))

    pairs = scope.ordered_pairs
    scope_params: dict[str, Any] = {
        "t": scope.tenant_id,
        "class_ids": [p[0] for p in pairs],
        "subject_ids": [p[1] for p in pairs],
        "statuses": list(scope.statuses),
    }
    count = int((await db.execute(text(count_sql()), scope_params)).scalar_one())
    if count == 0:
        return _result(config, model, has_scope=True, lexeme_count=len(lexemes))

    vector = _vector_literal(await query_embedder.embed(query), query_embedder.dim)
    extension = (await db.execute(text(VECTOR_EXTENSION_SQL))).first()
    if extension is None:
        raise RuntimeError("the pgvector extension is not installed in this database")
    schema, version = str(extension[0]), str(extension[1])

    hnsw = count > config.exact_max_candidates
    if hnsw:
        if _version_tuple(version) < MIN_PGVECTOR_FOR_HNSW:
            raise RuntimeError("the HNSW retrieval path needs pgvector 0.8.0 or newer (hnsw.iterative_scan)")
        applied = (
            await db.execute(text(HNSW_SETTINGS_SQL), {"mode": HNSW_ITERATIVE_SCAN, "ef": str(config.hnsw_ef_search)})
        ).one()[2]
        if applied != "relaxed_order":
            raise RuntimeError(f"hnsw.iterative_scan is {applied!r}, not relaxed_order: pgvector 0.8.0 or newer is needed")
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
                "kcap": config.keyword_max_candidates,
            },
        )
    ).mappings().all()

    by_id = {row["id"]: row for row in rows}
    on_vector_side = [r for r in rows if r["vector_rank"] is not None and _finite_or_none(r["vector_similarity"]) is not None]
    vector_ranked = tuple(r["id"] for r in sorted(on_vector_side, key=lambda r: r["vector_rank"]))
    keyword_ranked = tuple(r["id"] for r in sorted((r for r in rows if r["keyword_rank"] is not None), key=lambda r: r["keyword_rank"]))
    chosen = select_hits(rrf_fuse(vector_ranked, keyword_ranked, config.rrf_k), vector_ranked, final_k)

    hits = []
    for position, f in enumerate(chosen, start=1):
        row = by_id[f.id]
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
                vector_similarity=_finite_or_none(row["vector_similarity"]) if f.vector_rank is not None else None,
                keyword_rank=f.keyword_rank,
                keyword_matched_terms=row["matched"],
                fused_score=f.score,
                injection_flags=tuple(row["injection_flags"] or ()),
            )
        )

    max_similarity, coverage = hit_stats(hits, len(lexemes))
    return _result(
        config, model, has_scope=True, candidate_count=count, path="hnsw" if hnsw else "exact",
        max_similarity=max_similarity, keyword_coverage=coverage, lexeme_count=len(lexemes),
        hits=tuple(hits), vector_ranked=vector_ranked, keyword_ranked=keyword_ranked,
    )


async def search_for_chat(
    db: AsyncSession,
    scope: RetrievalScope,
    raw_query: str,
    query_embedder: QueryEmbedder,
    config: RetrievalConfig,
    *,
    k: int | None = None,
) -> RetrievalResult:
    """THE entry point for the chat layer (Phase 6). The scope must be a read scope of published versions;
    when the material is not sufficient the result carries NO hits and no ranked ids, so nothing reaches a
    model. Every text field of a returned hit is untrusted (see Hit)."""
    _require_issued(scope)
    if scope.purpose != "read" or scope.statuses != (VERSION_STATUS_PUBLISHED,):
        raise ValueError("the chat layer searches with a read scope of published versions only")
    result = await search(db, scope, raw_query, query_embedder, config, k=k)
    if not result.sufficient:
        return dataclasses.replace(result, hits=(), vector_ranked=(), keyword_ranked=())
    return result
