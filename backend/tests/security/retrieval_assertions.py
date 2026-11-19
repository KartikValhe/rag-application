"""
tests/security/retrieval_assertions.py
──────────────────────────────────────
Shared helpers and assertions for the retrieval tests (step 5d-1, design C2 "prove-it-fails"). The leak and
scope tests use them with every protection ON; tests/security/test_prove_it_fails_retrieval.py switches one
protection off at a time and shows that the SAME helper then raises.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_db import tenant_session_context
from app.db.models.user import User
from app.services import retrieval
from tests.fixtures.academic_world import WorldTenant


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


def assert_canary_absent(result: retrieval.RetrievalResult, canary: str, why: str) -> None:
    """No hit may contain `canary`."""
    leaked = [h for h in result.hits if canary in h.content]
    assert not leaked, f"retrieval leaked {canary!r} ({why}): version status {[h.version_status for h in leaked]}"


def assert_canary_present(result: retrieval.RetrievalResult, canary: str, why: str) -> None:
    assert any(canary in h.content for h in result.hits), f"{canary!r} missing ({why}); got {contents(result)}"


def assert_only_chunks(result: retrieval.RetrievalResult, allowed: set[uuid.UUID], why: str) -> None:
    """Every hit must be one of `allowed` (for example: the caller's own tenant's chunks)."""
    foreign = [h.chunk_id for h in result.hits if h.chunk_id not in allowed]
    assert not foreign, f"retrieval returned chunks outside the allowed set ({why}): {foreign}"
