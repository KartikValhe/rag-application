"""
tests/security/lifecycle_assertions.py
──────────────────────────────────────
Shared assertions for the document lifecycle (step 5c-2, design C2 "prove-it-fails"): the single-published
invariant. tests/security/test_prove_it_fails_pipeline.py switches the protection off (a publish that commits in
two steps) and shows that the SAME helper then raises.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text

from app.core.tenant_db import tenant_session_context
from app.services import document_lifecycle
from tests.conftest import _TestSession
from tests.fixtures.academic_world import WorldTenant

Publisher = Callable[[WorldTenant, uuid.UUID, uuid.UUID], Awaitable[None]]


def assert_exactly_one_published(counts: list[int]) -> None:
    """A reader must see exactly one published version of the document at every moment of a publish."""
    assert counts and all(c == 1 for c in counts), (
        f"a reader saw {counts} published versions of one document: zero means students lost the document for a "
        "moment, two means they could read two versions"
    )


async def _count_published(document_id: uuid.UUID) -> int:
    async with _TestSession() as reader:  # a second, independent connection
        value = (
            await reader.execute(
                text("SELECT count(*) FROM app.document_versions WHERE document_id = :d AND status = 'published'"),
                {"d": document_id},
            )
        ).scalar_one()
        return int(value)


async def single_transaction_publish(w: WorldTenant, document_id: uuid.UUID, version_id: uuid.UUID) -> None:
    """What the route and the worker do: the whole publish in ONE transaction."""
    async with tenant_session_context(str(w.id)) as db:
        await document_lifecycle.publish_version(
            db, tenant_id=w.id, document_id=document_id, version_id=version_id, actor_id=w.admin.id
        )


async def observe_published_counts_during_publish(
    w: WorldTenant, document_id: uuid.UUID, version_id: uuid.UUID, *, publisher: Publisher | None = None
) -> list[int]:
    """
    Published-version counts seen by a second connection: before the publish, in the middle (after the old
    version was archived and before the new one is marked published, the publisher is held at a barrier) and
    after it finished. `publisher` is the publish under test; by default the real one.
    """
    run = publisher or single_transaction_publish
    middle = asyncio.Event()
    observed = asyncio.Event()
    real: Any = document_lifecycle.mark_published

    async def held(*args: Any, **kwargs: Any) -> Any:
        middle.set()
        await asyncio.wait_for(observed.wait(), 30)
        return await real(*args, **kwargs)

    document_lifecycle.mark_published = held  # type: ignore[assignment]
    try:
        counts = [await _count_published(document_id)]
        task = asyncio.create_task(run(w, document_id, version_id))
        await asyncio.wait_for(middle.wait(), 30)
        counts.append(await _count_published(document_id))
        observed.set()
        await task
        counts.append(await _count_published(document_id))
    finally:
        document_lifecycle.mark_published = real  # type: ignore[assignment]
    return counts
