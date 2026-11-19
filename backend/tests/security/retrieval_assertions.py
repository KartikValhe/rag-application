"""
tests/security/retrieval_assertions.py
──────────────────────────────────────
Shared helpers and assertions for the retrieval tests (step 5d-1, design C2 "prove-it-fails"). The leak and
scope tests use them with every protection ON; tests/security/test_prove_it_fails_retrieval.py switches one
protection off at a time and shows that the SAME helper then raises.

A leak check looks at EVERY id the search produced: the hits AND the vector side's ranked list AND the
keyword side's ranked list. A chunk below the final cut (rank 7 and lower) is still a leak. Each check
also asserts that neither ranked list was cut at its limit (vector_k / keyword_k), so a leaked chunk can
not hide below the ranked lists either: the vector side then lists every candidate with an active-model
vector, and the keyword side every candidate that matches a query word.
"""

import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db.models.user import User
from app.services import retrieval
from tests.fixtures.academic_world import WorldTenant
from tests.fixtures.retrieval_world import SeededDocument


async def run_search(
    wt: WorldTenant,
    user: User,
    query: str,
    embedder: Any,
    *,
    purpose: retrieval.Purpose = "manage",
    include_drafts: bool = False,
    class_id: uuid.UUID | None = None,
    subject_id: uuid.UUID | None = None,
    config: retrieval.RetrievalConfig | None = None,
    k: int | None = None,
) -> retrieval.RetrievalResult:
    """Build the caller's scope and search, both in ONE tenant transaction as app_tenant (RLS on)."""
    async with tenant_session_context(str(wt.id)) as db:
        scope = await retrieval.build_retrieval_scope(
            db, user, purpose=purpose, class_id=class_id, subject_id=subject_id, include_drafts=include_drafts
        )
        return await retrieval.search(
            db, scope, query, retrieval.QueryEmbedder.from_embedder(embedder),
            config or retrieval.RetrievalConfig.from_settings(), k=k,
        )


async def search_in_session(
    db: AsyncSession, scope: retrieval.RetrievalScope, query: str, embedder: Any,
    config: retrieval.RetrievalConfig | None = None,
) -> retrieval.RetrievalResult:
    """Search in a session the caller opened (the prove-it test passes a privileged session here)."""
    return await retrieval.search(
        db, scope, query, retrieval.QueryEmbedder.from_embedder(embedder), config or retrieval.RetrievalConfig.from_settings()
    )


def contents(result: retrieval.RetrievalResult) -> list[str]:
    return [h.content for h in result.hits]


def every_id(result: retrieval.RetrievalResult) -> set[uuid.UUID]:
    """Every chunk id the search produced: hits, vector side and keyword side."""
    return {h.chunk_id for h in result.hits} | set(result.vector_ranked) | set(result.keyword_ranked)


def _ids(chunks: Iterable[uuid.UUID] | SeededDocument) -> set[uuid.UUID]:
    if isinstance(chunks, SeededDocument):
        return {c.id for c in chunks.chunks}
    return set(chunks)


def assert_nothing_hidden(result: retrieval.RetrievalResult, config: retrieval.RetrievalConfig | None = None) -> None:
    """Neither ranked list reached its limit, so every chunk the search could reach is in one of them."""
    cfg = config or retrieval.RetrievalConfig.from_settings()
    assert len(result.vector_ranked) < cfg.vector_k, "the vector side was cut: a leaked chunk could hide below it"
    assert len(result.keyword_ranked) < cfg.keyword_k, "the keyword side was cut: a leaked chunk could hide below it"


def assert_absent(
    result: retrieval.RetrievalResult,
    forbidden: Iterable[uuid.UUID] | SeededDocument,
    why: str,
    *,
    canary: str | None = None,
) -> None:
    """None of `forbidden` appears anywhere in the result (hits, vector side, keyword side), and no hit
    contains `canary`. The leak is checked first, so a leak always fails with "leaked" in the message."""
    leaked = every_id(result) & _ids(forbidden)
    statuses = [h.version_status for h in result.hits if h.chunk_id in leaked]
    assert not leaked, f"retrieval leaked {len(leaked)} chunk(s) ({why}); hit version statuses {statuses}"
    if canary is not None:
        in_hits = [h for h in result.hits if canary in h.content]
        assert not in_hits, f"retrieval leaked {canary!r} ({why})"
    assert_nothing_hidden(result)


def assert_present(result: retrieval.RetrievalResult, chunk_id: uuid.UUID, why: str) -> None:
    assert chunk_id in {h.chunk_id for h in result.hits}, f"chunk missing from the hits ({why}); got {contents(result)}"


def assert_canary_present(result: retrieval.RetrievalResult, canary: str, why: str) -> None:
    assert any(canary in h.content for h in result.hits), f"{canary!r} missing ({why}); got {contents(result)}"


def assert_only_chunks(result: retrieval.RetrievalResult, allowed: Iterable[uuid.UUID], why: str) -> None:
    """Every id the search produced (hits and both ranked lists) must be one of `allowed`."""
    foreign = every_id(result) - set(allowed)
    assert not foreign, f"retrieval leaked chunks outside the allowed set ({why}): {foreign}"
    assert_nothing_hidden(result)
